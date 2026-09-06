import { useEffect, useState } from "react";
import { useParams, useNavigate, Link } from "react-router-dom";
import {
  documentsApi,
  sourcesApi,
  type Document,
  type DocumentStatus,
  type SourceResponse,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { formatBytes, formatDate } from "@/lib/utils";
import {
  RiArrowLeftLine,
  RiFileTextLine,
  RiCheckboxCircleLine,
  RiErrorWarningLine,
  RiLoader4Line,
  RiDeleteBinLine,
  RiPagesLine,
  RiFileCodeLine,
  RiDatabase2Line,
  RiInformationLine,
} from "react-icons/ri";

export default function DocumentDetail() {
  const { id } = useParams<{ id: string }>();
  const nav = useNavigate();
  const [doc, setDoc] = useState<Document | null>(null);
  const [status, setStatus] = useState<DocumentStatus | null>(null);
  const [source, setSource] = useState<SourceResponse | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!id) return;
    setLoading(true);
    Promise.all([
      documentsApi.get(id),
      documentsApi.status(id).catch(() => null),
      sourcesApi.get(id).catch(() => null),
    ]).then(([d, s, src]) => {
      setDoc(d);
      setStatus(s);
      setSource(src);
      setLoading(false);
    });
  }, [id]);

  const doDelete = async () => {
    if (!id || !confirm("Delete this document permanently?")) return;
    await documentsApi.remove(id);
    nav("/documents");
  };

  if (loading) {
    return (
      <div className="flex h-64 items-center justify-center">
        <RiLoader4Line size={20} className="animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (!doc) {
    return (
      <div className="p-6 text-center text-muted-foreground text-sm">
        Document not found
      </div>
    );
  }

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="shrink-0 border-b px-6 py-4">
        <Link
          to="/documents"
          className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground mb-3"
        >
          <RiArrowLeftLine size={12} /> Documents
        </Link>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3 min-w-0">
            <RiFileTextLine size={18} className="shrink-0 text-muted-foreground" />
            <div className="min-w-0">
              <h1 className="text-lg font-semibold truncate">{doc.title}</h1>
              <p className="text-xs text-muted-foreground">{doc.filename}</p>
            </div>
          </div>
          <Button variant="destructive" size="sm" onClick={doDelete}>
            <RiDeleteBinLine size={14} /> Delete
          </Button>
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-auto p-6">
        <div className="max-w-3xl mx-auto space-y-6">
          {/* Pipeline status */}
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-sm flex items-center gap-2">
                <RiInformationLine size={14} className="text-muted-foreground" />
                Pipeline
              </CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              {/* Stage indicator */}
              <div className="flex items-center gap-3">
                {status?.stage === "ready" ? (
                  <RiCheckboxCircleLine size={18} className="text-green-600" />
                ) : status?.stage === "failed" ? (
                  <RiErrorWarningLine size={18} className="text-red-600" />
                ) : (
                  <RiLoader4Line size={18} className="animate-spin text-muted-foreground" />
                )}
                <span className="text-sm font-medium capitalize">
                  {status?.stage ?? doc.status}
                </span>
              </div>

              {/* Counts */}
              {status?.counts && (
                <div className="grid grid-cols-3 gap-3">
                  <div className="rounded-md bg-muted px-3 py-2.5 text-center">
                    <RiPagesLine size={14} className="mx-auto text-muted-foreground mb-1" />
                    <p className="text-lg font-bold leading-none">{status.counts.pages}</p>
                    <p className="text-[10px] text-muted-foreground mt-1">Pages</p>
                  </div>
                  <div className="rounded-md bg-muted px-3 py-2.5 text-center">
                    <RiFileCodeLine size={14} className="mx-auto text-muted-foreground mb-1" />
                    <p className="text-lg font-bold leading-none">{status.counts.chunks}</p>
                    <p className="text-[10px] text-muted-foreground mt-1">Chunks</p>
                  </div>
                  <div className="rounded-md bg-muted px-3 py-2.5 text-center">
                    <RiDatabase2Line size={14} className="mx-auto text-muted-foreground mb-1" />
                    <p className="text-lg font-bold leading-none">{status.counts.embeddings}</p>
                    <p className="text-[10px] text-muted-foreground mt-1">Vectors</p>
                  </div>
                </div>
              )}

              {/* Pipeline stages */}
              <div className="space-y-1.5 text-xs">
                <Row label="Extraction">
                  <StatusDot status={doc.status} />
                </Row>
                <Row label="Embedding">
                  <StatusDot status={doc.embeddingStatus} />
                </Row>
              </div>

              {doc.error && (
                <div className="rounded-md bg-destructive/10 px-3 py-2 text-xs text-destructive">
                  {doc.error}
                </div>
              )}
            </CardContent>
          </Card>

          {/* Metadata */}
          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-sm flex items-center gap-2">
                <RiInformationLine size={14} className="text-muted-foreground" />
                Metadata
              </CardTitle>
            </CardHeader>
            <CardContent>
              <div className="space-y-1.5 text-xs">
                <Row label="Size">{formatBytes(doc.size)}</Row>
                <Row label="Type">
                  <span className="uppercase font-medium">{doc.sourceType}</span>
                </Row>
                <Row label="MIME">{doc.mimeType}</Row>
                <Row label="Created">{formatDate(doc.createdAt)}</Row>
                <Row label="Updated">{formatDate(doc.updatedAt)}</Row>
                <Row label="Document ID">
                  <span className="font-mono text-[11px]">{doc.id}</span>
                </Row>
                {source && (
                  <Row label="Pages extracted">{source.pages}</Row>
                )}
              </div>
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
}

function Row({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between py-1.5">
      <span className="text-muted-foreground">{label}</span>
      <span>{children}</span>
    </div>
  );
}

function StatusDot({ status }: { status: string }) {
  const colors: Record<string, string> = {
    ready: "bg-green-500",
    processing: "bg-yellow-500",
    queued: "bg-zinc-400",
    failed: "bg-red-500",
    none: "bg-zinc-300",
  };
  return (
    <span className="inline-flex items-center gap-1.5 text-xs font-medium capitalize">
      <span className={`h-1.5 w-1.5 rounded-full ${colors[status] ?? "bg-zinc-300"}`} />
      {status}
    </span>
  );
}
