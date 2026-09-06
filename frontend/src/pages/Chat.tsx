import { useCallback, useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import {
  conversationsApi,
  ragApi,
  type Citation,
  type Conversation,
  type Message,
  type RagResponse,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ScrollArea } from "@/components/ui/scroll-area";
import { formatRelative } from "@/lib/utils";
import { cn } from "@/lib/utils";
import {
  RiSendPlaneLine,
  RiAddLine,
  RiDeleteBinLine,
  RiLoader4Line,
  RiRobot2Line,
  RiUser3Line,
  RiChat3Line,
  RiErrorWarningLine,
  RiSidebarFoldLine,
  RiCloseLine,
} from "react-icons/ri";

interface StreamEntry {
  question: string;
  answer: string;
  citations: Citation[];
  refused: boolean;
  streaming: boolean;
  error?: string;
}

const EMPTY_CONVERSATION: Conversation = {
  id: "",
  organizationId: "",
  title: "",
  createdBy: "",
  createdAt: "",
  updatedAt: "",
  messageCount: 0,
  lastMessageAt: null,
};

export default function Chat() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [persistedMessages, setPersistedMessages] = useState<Message[]>([]);
  const [streams, setStreams] = useState<StreamEntry[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showSidebar, setShowSidebar] = useState(true);
  const setCurrentId = (id: string | null) => {
    activeIdRef.current = id;
    setActiveId(id);
  };
  const [selectedCitation, setSelectedCitation] = useState<number | null>(null);
  // Confidence floor on the strongest evidence; below it the guardrail
  // answers the canonical "I don't know." instead of a weak match. Off by
  // default (recall-first); the demo floor is 0.20.
  const [minScore, setMinScore] = useState<number | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const abortRef = useRef<(() => void) | null>(null);
  // Synchronous in-flight guard. `busy` state updates are async — a rapid
  // double activation (Enter held down, or Enter + click in the same tick)
  // can both pass a `busy === false` check before React re-renders, firing
  // two SSE streams and persisting the same message twice. A ref is set
  // (and read) synchronously so the second call bails immediately.
  const busyRef = useRef(false);
  // Current conversation id mirrored in a ref so an in-flight send persists
  // against the freshest value instead of a stale render-time closure. This
  // stops a doubled first message from spawning a second brand-new
  // conversation when the closure captured `activeId === null`.
  const activeIdRef = useRef<string | null>(null);

  // load conversation list once
  useEffect(() => {
    conversationsApi.list({ limit: 100 }).then((r) => setConversations(r.items));
  }, []);

  // load messages when a conversation is selected
  const loadConversation = useCallback(async (id: string) => {
    setCurrentId(id);
    setStreams([]);
    setPersistedMessages([]);
    setError(null);
    try {
      const c = await conversationsApi.get(id);
      setPersistedMessages(c.messages ?? []);
    } catch {
      setError("Could not load this conversation");
    }
  }, []);

  const newChat = async () => {
    setCurrentId(null);
    setPersistedMessages([]);
    setStreams([]);
    setError(null);
    inputRef.current?.focus();
  };

  const persistNewConversation = useCallback(
    async (
      text: string,
      assistant: {
        content: string;
        refused: boolean;
        provider: string;
        model: string;
        citations: Citation[];
        usage: { promptTokens: number; completionTokens: number; totalTokens: number };
      }
    ) => {
      const created = await conversationsApi.create();
      setCurrentId(created.id);
      setConversations((prev) => [created, ...prev]);
      return conversationsApi.addMessage(created.id, text, assistant);
    },
    []
  );

  const deleteChat = async (id: string) => {
    if (!confirm("Delete this conversation?")) return;
    await conversationsApi.remove(id);
    setConversations((prev) => prev.filter((c) => c.id !== id));
    if (activeId === id) {
      setCurrentId(null);
      setPersistedMessages([]);
    }
  };

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [persistedMessages, streams]);

  const ask = async () => {
    const text = input.trim();
    if (!text || busy || busyRef.current) return;
    // set the synchronous lock immediately — the `busy` state below won't be
    // visible to a second activation until React re-renders
    busyRef.current = true;
    setInput("");
    setError(null);

    // keep the last few persisted messages as stream history for referential
    // continuity (optional). We just stream the one question now.
    const entry: StreamEntry = {
      question: text,
      answer: "",
      citations: [],
      refused: false,
      streaming: true,
    };
    setStreams((prev) => [...prev, entry]);
    setBusy(true);

    const handle = ragApi.generateStream(
      text,
      minScore != null ? { minScore } : undefined
    );
    abortRef.current = handle.abort;
    try {
      const { deltas, done } = await handle.read();
      setStreams((prev) =>
        prev.map((e, i) => {
          if (e.question !== text) return e;
          return {
            ...e,
            answer: deltas || done?.answer || "",
            citations: done?.citations ?? [],
            refused: done?.refused ?? false,
            streaming: false,
          };
        })
      );

      // persist the exact answer the user just read; auto-create a
      // conversation on the first message
      if (done) {
        const assistant = {
          content: done.answer,
          refused: done.refused,
          provider: done.provider,
          model: done.model,
          citations: done.citations,
          usage: done.usage,
        };
        const updated = activeIdRef.current
          ? await conversationsApi.addMessage(activeIdRef.current, text, assistant)
          : await persistNewConversation(text, assistant);
        setPersistedMessages(updated.messages ?? []);
        // the turn now lives in persistedMessages — drop the streamed copy
        // so the question isn't rendered twice
        setStreams((prev) =>
          prev.filter((e) => !(e.question === text && !e.streaming))
        );
        // refresh the sidebar so it reflects server ordering (updated_at DESC)
        conversationsApi
          .list({ limit: 100 })
          .then((r) => setConversations(r.items));
      }
    } catch (e) {
      const msg = e instanceof Error ? e.message : "Something went wrong";
      setStreams((prev) =>
        prev.map((en) =>
          en.question === text
            ? { ...en, streaming: false, error: msg }
            : en
        )
      );
    } finally {
      setBusy(false);
      busyRef.current = false;
      abortRef.current = null;
      inputRef.current?.focus();
    }
  };

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      ask();
    }
  };

  const stopStreaming = () => {
    abortRef.current?.();
    setBusy(false);
  };

  const allEmpty =
    persistedMessages.length === 0 && streams.length === 0 && !error;

  return (
    <div className="flex h-full">
      {/* Conversation sidebar */}
      {showSidebar && (
        <div className="w-64 border-r bg-sidebar flex flex-col shrink-0">
          <div className="p-3 border-b">
            <Button onClick={newChat} className="w-full" size="sm">
              <RiAddLine size={14} /> New chat
            </Button>
          </div>
          <ScrollArea className="flex-1 p-2">
            {conversations.length === 0 ? (
              <p className="px-3 py-2 text-xs text-muted-foreground">
                No conversations yet
              </p>
            ) : (
              conversations.map((c) => (
                <div
                  key={c.id}
                  className={cn(
                    "group flex items-center gap-2 rounded-md px-3 py-2 text-sm cursor-pointer transition-colors mb-1",
                    activeId === c.id
                      ? "bg-primary text-primary-foreground"
                      : "hover:bg-accent text-foreground"
                  )}
                  onClick={() => loadConversation(c.id)}
                >
                  <RiChat3Line size={14} className="shrink-0 opacity-50" />
                  <div className="min-w-0 flex-1">
                    <p className="truncate">{c.title || "New conversation"}</p>
                    {c.lastMessageAt && (
                      <p className="truncate text-[10px] opacity-60">
                        {formatRelative(c.lastMessageAt)}
                      </p>
                    )}
                  </div>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      deleteChat(c.id);
                    }}
                    className="opacity-0 group-hover:opacity-100 text-muted-foreground hover:text-destructive transition-opacity p-0.5"
                    title="Delete"
                  >
                    <RiDeleteBinLine size={12} />
                  </button>
                </div>
              ))
            )}
          </ScrollArea>
        </div>
      )}

      {/* Main area */}
      <div className="flex-1 flex flex-col min-w-0">
        {/* Top bar */}
        <div className="flex items-center gap-2 border-b px-4 h-12 shrink-0">
          <button
            onClick={() => setShowSidebar((s) => !s)}
            className="p-1.5 rounded text-muted-foreground hover:bg-accent"
            title={showSidebar ? "Hide sidebar" : "Show sidebar"}
          >
            <RiSidebarFoldLine size={18} />
          </button>
          <span className="text-sm font-medium truncate">
            {activeId
              ? conversations.find((c) => c.id === activeId)?.title ||
                "Conversation"
              : "New chat"}
          </span>
          {activeId && (
            <button
              onClick={() => {
                setCurrentId(null);
                setPersistedMessages([]);
              }}
              className="ml-auto p-1.5 rounded text-muted-foreground hover:bg-accent"
              title="Close"
            >
              <RiCloseLine size={16} />
            </button>
          )}
        </div>

        {/* Messages / answers */}
        <ScrollArea className="flex-1">
          {allEmpty ? (
            <EmptyState />
          ) : (
            <div className="max-w-2xl mx-auto px-6 py-8 space-y-8">
              {persistedMessages
                .filter((m) => m.role !== "system")
                .map((m) => {
                  const citations = (m.payload?.citations as Citation[]) ?? [];
                  if (m.role === "user") {
                    return (
                      <MessageRow
                        key={m.id}
                        role="user"
                        content={m.content}
                      />
                    );
                  }
                  return (
                    <div key={m.id}>
                      <p className="text-xs font-medium text-muted-foreground mb-1">
                        Answer
                      </p>
                      <div className="h-px bg-border mb-3" />
                      <MarkdownContent
                        text={m.content}
                        citations={citations}
                        selectedCitation={selectedCitation}
                      />
                      {citations.length > 0 && (
                        <CitationList
                          citations={citations}
                          selectedCitation={selectedCitation}
                          onSelect={(n) => setSelectedCitation(n)}
                        />
                      )}
                    </div>
                  );
                })}

              {streams.map((s, i) => (
                <StreamBlock
                  key={`stream-${i}-${s.question}`}
                  entry={s}
                  selectedCitation={selectedCitation}
                  onSelectCitation={(n) => setSelectedCitation(n)}
                />
              ))}

              {busy && (
                <div className="flex items-center gap-3 text-muted-foreground">
                  <RiLoader4Line size={18} className="animate-spin" />
                  <span className="text-sm">Searching your documents...</span>
                  <button
                    onClick={stopStreaming}
                    className="ml-auto text-xs text-muted-foreground hover:text-foreground"
                  >
                    Stop
                  </button>
                </div>
              )}

              {error && (
                <div className="flex items-center gap-2 rounded-md bg-destructive/10 px-4 py-3 text-sm text-destructive">
                  <RiErrorWarningLine size={16} />
                  {error}
                </div>
              )}

              <div ref={bottomRef} />
            </div>
          )}
        </ScrollArea>

        {/* Input */}
        <div className="shrink-0 border-t bg-background">
          <div className="max-w-2xl mx-auto px-6 py-4">
            <div className="flex gap-2 items-end">
              <Textarea
                ref={inputRef}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={onKey}
                placeholder="Ask your knowledge base"
                rows={2}
                className="resize-none text-sm"
              />
              <Button
                onClick={ask}
                disabled={!input.trim() || busy}
                size="icon"
                className="shrink-0 h-10 w-10"
              >
                <RiSendPlaneLine size={16} />
              </Button>
            </div>
            <p className="text-[10px] text-muted-foreground mt-2">
              Answers are grounded in your uploaded documents and cite sources.
            </p>
            <label className="mt-1 flex items-center gap-1.5 text-[10px] text-muted-foreground cursor-pointer select-none">
              <input
                type="checkbox"
                checked={minScore != null}
                onChange={(e) => setMinScore(e.target.checked ? 0.2 : null)}
                className="accent-primary h-3 w-3"
              />
              Refuse weak matches (similarity floor 0.20)
            </label>
          </div>
        </div>
      </div>
    </div>
  );
}

function EmptyState() {
  return (
    <div className="flex h-full flex-col items-center justify-center text-center px-6">
      <RiRobot2Line size={48} className="text-muted-foreground/40 mb-4" />
      <h1 className="text-xl font-semibold mb-2">Ask your knowledge base</h1>
      <p className="text-sm text-muted-foreground max-w-sm">
        Ask a question and get a grounded answer with cited sources from your
        uploaded documents.
      </p>
    </div>
  );
}

function MessageRow({ role, content }: { role: "user" | "assistant"; content: string }) {
  return (
    <div className={cn("flex gap-3", role === "user" && "justify-end")}>
      {role === "assistant" && (
        <div className="rounded-full bg-primary p-2 text-primary-foreground shrink-0 mt-1">
          <RiRobot2Line size={16} />
        </div>
      )}
      <div
        className={cn(
          "max-w-[80%] rounded-lg px-4 py-3 text-sm whitespace-pre-wrap",
          role === "user" ? "bg-primary text-primary-foreground" : "bg-muted"
        )}
      >
        {content}
      </div>
      {role === "user" && (
        <div className="rounded-full bg-muted p-2 shrink-0 mt-1">
          <RiUser3Line size={16} />
        </div>
      )}
    </div>
  );
}

function StreamBlock({
  entry,
  selectedCitation,
  onSelectCitation,
}: {
  entry: StreamEntry;
  selectedCitation: number | null;
  onSelectCitation: (n: number) => void;
}) {
  return (
    <div className="space-y-2">
      <MessageRow role="user" content={entry.question} />
      <div>
        <p className="text-xs font-medium text-muted-foreground mb-1">Answer</p>
        <div className="h-px bg-border mb-3" />
        {entry.answer === "" && entry.streaming && !entry.error ? (
          <div className="flex items-center gap-2 text-muted-foreground py-2">
            <RiLoader4Line size={16} className="animate-spin" />
            <span className="text-sm">Thinking...</span>
          </div>
        ) : (
          <MarkdownContent
            text={entry.answer}
            citations={entry.citations}
            selectedCitation={selectedCitation}
          />
        )}
        {entry.error && (
          <div className="flex items-center gap-2 rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive mt-2">
            <RiErrorWarningLine size={16} />
            {entry.error}
          </div>
        )}
        {!entry.streaming && entry.citations.length > 0 && (
          <CitationList
            citations={entry.citations}
            selectedCitation={selectedCitation}
            onSelect={onSelectCitation}
          />
        )}
      </div>
    </div>
  );
}

function MarkdownContent({
  text,
  citations,
  selectedCitation,
}: {
  text: string;
  citations: Citation[];
  selectedCitation: number | null;
}) {
  // Render [n] markers as inline badges merged around markdown.
  const parts = text.split(/(\[\d+\])/g);
  return (
    <div className="space-y-2 text-sm leading-relaxed">
      {parts.map((part, i) => {
        const m = part.match(/^\[(\d+)\]$/);
        if (m) {
          const n = parseInt(m[1]);
          const cited = citations.some((c) => c.id === n);
          if (cited) {
            return (
              <sup
                key={i}
                className={cn(
                  "inline-flex items-center rounded bg-primary/10 px-1.5 py-0.5 text-xs font-semibold text-primary leading-none mx-0.5",
                  selectedCitation === n && "bg-primary text-primary-foreground"
                )}
              >
                {n}
              </sup>
            );
          }
        }
        // Return markdown-rendered segment
        return <MarkdownSegment key={i} text={part} />;
      })}
    </div>
  );
}

function MarkdownSegment({ text }: { text: string }) {
  return (
    <Markdown>
      {text}
    </Markdown>
  );
}

function Markdown({ children }: { children: string }) {
  return (
    <span className="[&_p]:my-1 [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:list-decimal [&_ol]:pl-5 [&_strong]:font-semibold [&_code]:rounded [&_code]:bg-muted [&_code]:px-1 [&_code]:py-0.5 [&_code]:text-[0.85em] [&_pre]:rounded-md [&_pre]:bg-muted [&_pre]:p-3 [&_a]:text-primary [&_a]:underline">
      <ReactMarkdown>{children}</ReactMarkdown>
    </span>
  );
}

function CitationList({
  citations,
  selectedCitation,
  onSelect,
}: {
  citations: Citation[];
  selectedCitation: number | null;
  onSelect: (n: number) => void;
}) {
  return (
    <div className="mt-4">
      <p className="text-xs font-medium text-muted-foreground mb-1">Sources</p>
      <div className="h-px bg-border mb-3" />
      <div className="space-y-1.5">
        {citations.map((cit) => (
          <button
            key={cit.id}
            onClick={() => onSelect(cit.id)}
            className={cn(
              "flex w-full items-start gap-3 rounded-md px-2 py-1.5 text-left text-sm hover:bg-accent transition-colors",
              selectedCitation === cit.id && "bg-accent"
            )}
          >
            <span className="text-muted-foreground font-mono text-xs mt-0.5 shrink-0">
              [{cit.id}]
            </span>
            <span>
              <span className="font-medium">{cit.title ?? "Untitled"}</span>
              {cit.page != null && (
                <span className="text-muted-foreground ml-2">
                  Page {cit.page}
                </span>
              )}
              {cit.section && (
                <span className="text-muted-foreground ml-2">{cit.section}</span>
              )}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
