import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { documentsApi, conversationsApi, type Document, type Conversation } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { formatBytes, formatRelative } from "@/lib/utils";
import {
  RiFileTextLine,
  RiChat3Line,
  RiArrowRightLine,
  RiCheckboxCircleLine,
  RiTimeLine,
  RiErrorWarningLine,
  RiLoader4Line,
} from "react-icons/ri";

const stageIcon = (stage: string) => {
  switch (stage) {
    case "ready":
      return <RiCheckboxCircleLine className="text-green-600" size={16} />;
    case "failed":
      return <RiErrorWarningLine className="text-red-600" size={16} />;
    default:
      return <RiLoader4Line className="animate-spin text-muted-foreground" size={16} />;
  }
};

export default function Dashboard() {
  const [docs, setDocs] = useState<Document[]>([]);
  const [docTotal, setDocTotal] = useState(0);
  const [convos, setConvos] = useState<Conversation[]>([]);
  const [convoTotal, setConvoTotal] = useState(0);

  useEffect(() => {
    documentsApi.list({ limit: 5 }).then((r) => {
      setDocs(r.items);
      setDocTotal(r.pagination.total);
    });
    conversationsApi.list({ limit: 5 }).then((r) => {
      setConvos(r.items);
      setConvoTotal(r.pagination.total);
    });
  }, []);

  return (
    <div className="p-6 max-w-6xl mx-auto space-y-6">
      <h1 className="text-2xl font-bold">Dashboard</h1>

      {/* Stats */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <Card>
          <CardContent className="flex items-center gap-4 p-6">
            <div className="rounded-md bg-primary/10 p-3">
              <RiFileTextLine size={20} className="text-primary" />
            </div>
            <div>
              <p className="text-2xl font-bold">{docTotal}</p>
              <p className="text-sm text-muted-foreground">Documents</p>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="flex items-center gap-4 p-6">
            <div className="rounded-md bg-primary/10 p-3">
              <RiChat3Line size={20} className="text-primary" />
            </div>
            <div>
              <p className="text-2xl font-bold">{convoTotal}</p>
              <p className="text-sm text-muted-foreground">Conversations</p>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="flex items-center gap-4 p-6">
            <div className="rounded-md bg-green-100 p-3">
              <RiCheckboxCircleLine size={20} className="text-green-600" />
            </div>
            <div>
              <p className="text-2xl font-bold">
                {docs.filter((d) => d.status === "ready" && d.embeddingStatus === "ready").length}
              </p>
              <p className="text-sm text-muted-foreground">Ready to search</p>
            </div>
          </CardContent>
        </Card>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Recent documents */}
        <Card>
          <CardHeader className="flex flex-row items-center justify-between">
            <CardTitle className="text-base">Recent Documents</CardTitle>
            <Link to="/documents">
              <Button variant="ghost" size="sm">
                View all <RiArrowRightLine size={14} />
              </Button>
            </Link>
          </CardHeader>
          <CardContent>
            {docs.length === 0 ? (
              <p className="text-sm text-muted-foreground py-4 text-center">
                No documents yet. Upload your first file to get started.
              </p>
            ) : (
              <div className="space-y-3">
                {docs.map((doc) => (
                  <Link
                    key={doc.id}
                    to={`/documents/${doc.id}`}
                    className="flex items-center justify-between rounded-md border p-3 hover:bg-accent transition-colors"
                  >
                    <div className="flex items-center gap-3 min-w-0">
                      <RiFileTextLine size={16} className="shrink-0 text-muted-foreground" />
                      <div className="min-w-0">
                        <p className="truncate text-sm font-medium">{doc.title}</p>
                        <p className="text-xs text-muted-foreground">
                          {formatBytes(doc.size)} · {doc.sourceType}
                        </p>
                      </div>
                    </div>
                    <div className="flex items-center gap-2 shrink-0">
                      {stageIcon(doc.status)}
                      <span className="text-xs text-muted-foreground whitespace-nowrap">
                        {formatRelative(doc.createdAt)}
                      </span>
                    </div>
                  </Link>
                ))}
              </div>
            )}
          </CardContent>
        </Card>

        {/* Recent conversations */}
        <Card>
          <CardHeader className="flex flex-row items-center justify-between">
            <CardTitle className="text-base">Recent Chats</CardTitle>
            <Link to="/chat">
              <Button variant="ghost" size="sm">
                Open chat <RiArrowRightLine size={14} />
              </Button>
            </Link>
          </CardHeader>
          <CardContent>
            {convos.length === 0 ? (
              <p className="text-sm text-muted-foreground py-4 text-center">
                No conversations yet. Start a chat to ask questions.
              </p>
            ) : (
              <div className="space-y-3">
                {convos.map((c) => (
                  <Link
                    key={c.id}
                    to={`/chat?id=${c.id}`}
                    className="flex items-center justify-between rounded-md border p-3 hover:bg-accent transition-colors"
                  >
                    <div className="flex items-center gap-3 min-w-0">
                      <RiChat3Line size={16} className="shrink-0 text-muted-foreground" />
                      <div className="min-w-0">
                        <p className="truncate text-sm font-medium">{c.title || "New conversation"}</p>
                        <p className="text-xs text-muted-foreground">
                          {c.messageCount} message{c.messageCount !== 1 ? "s" : ""}
                        </p>
                      </div>
                    </div>
                    <span className="text-xs text-muted-foreground whitespace-nowrap">
                      {formatRelative(c.updatedAt)}
                    </span>
                  </Link>
                ))}
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
