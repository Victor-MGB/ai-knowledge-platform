import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { documentsApi, type Document, type Pagination } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Select } from "@/components/ui/select";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { formatBytes, formatRelative } from "@/lib/utils";
import {
  RiUploadCloud2Line,
  RiFileTextLine,
  RiSearchLine,
  RiCheckboxCircleLine,
  RiErrorWarningLine,
  RiLoader4Line,
  RiDeleteBinLine,
  RiInformationLine,
  RiCalendarLine,
  RiFileZipLine,
} from "react-icons/ri";

function StatusDot({ status }: { status: string }) {
  const colors: Record<string, string> = {
    ready: "bg-green-500",
    processing: "bg-yellow-500",
    queued: "bg-zinc-400",
    failed: "bg-red-500",
    none: "bg-zinc-300",
  };
  return (
    <span className="inline-flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide">
      <span className={`h-1.5 w-1.5 rounded-full ${colors[status] ?? "bg-zinc-300"}`} />
      {status}
    </span>
  );
}

export default function Documents() {
  const [docs, setDocs] = useState<Document[]>([]);
  const [pagination, setPagination] = useState<Pagination>({
    limit: 20,
    offset: 0,
    total: 0,
    hasMore: false,
  });
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [typeFilter, setTypeFilter] = useState("");
  const [uploading, setUploading] = useState(false);
  const [uploadPct, setUploadPct] = useState(0);
  const [uploadDialog, setUploadDialog] = useState(false);
  const [dragOver, setDragOver] = useState(false);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  const load = useCallback(
    (offset = 0) => {
      documentsApi
        .list({
          limit: 20,
          offset,
          search: search || undefined,
          status: statusFilter || undefined,
          sourceType: typeFilter || undefined,
        })
        .then((r) => {
          setDocs(offset === 0 ? r.items : (prev) => [...prev, ...r.items]);
          setPagination(r.pagination);
        });
    },
    [search, statusFilter, typeFilter],
  );

  useEffect(() => {
    const t = setTimeout(() => load(0), 300);
    return () => clearTimeout(t);
  }, [load]);

  const doUpload = async (file: File) => {
    setUploading(true);
    setUploadPct(0);
    setUploadDialog(true);
    try {
      await documentsApi.upload(file, setUploadPct);
      load(0);
    } catch {
      // handled by dialog
    } finally {
      setUploading(false);
    }
  };

  const onFiles = (files: FileList | null) => {
    if (files?.[0]) doUpload(files[0]);
  };

  const onDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    onFiles(e.dataTransfer.files);
  };

  const doDelete = async (id: string) => {
    if (!confirm("Delete this document?")) return;
    await documentsApi.remove(id);
    if (expandedId === id) setExpandedId(null);
    load(0);
  };

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="shrink-0 border-b px-6 py-4">
        <div className="flex items-center justify-between mb-4">
          <h1 className="text-lg font-semibold">Documents</h1>
          <Button size="sm" onClick={() => fileRef.current?.click()}>
            <RiUploadCloud2Line size={14} /> Upload
          </Button>
          <input
            ref={fileRef}
            type="file"
            accept=".pdf,.docx,.md,.html,.txt"
            onChange={(e) => onFiles(e.target.files)}
            className="hidden"
          />
        </div>

        {/* Search + filters */}
        <div className="flex items-center gap-2">
          <div className="relative flex-1 max-w-xs">
            <RiSearchLine className="absolute left-2.5 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-muted-foreground" />
            <Input
              placeholder="Search..."
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="pl-8 h-8 text-sm"
            />
          </div>
          <Select
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
            className="w-32 h-8 text-xs"
          >
            <option value="">All statuses</option>
            <option value="ready">Ready</option>
            <option value="processing">Processing</option>
            <option value="queued">Queued</option>
            <option value="failed">Failed</option>
          </Select>
          <Select
            value={typeFilter}
            onChange={(e) => setTypeFilter(e.target.value)}
            className="w-28 h-8 text-xs"
          >
            <option value="">All types</option>
            <option value="pdf">PDF</option>
            <option value="docx">DOCX</option>
            <option value="md">Markdown</option>
            <option value="html">HTML</option>
            <option value="txt">Text</option>
          </Select>
          {(search || statusFilter || typeFilter) && (
            <Button
              variant="ghost"
              size="sm"
              className="h-8 text-xs"
              onClick={() => {
                setSearch("");
                setStatusFilter("");
                setTypeFilter("");
              }}
            >
              Clear
            </Button>
          )}
        </div>
      </div>

      {/* Drop zone overlay */}
      {dragOver && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-background/80 backdrop-blur-sm"
          onDragOver={(e) => e.preventDefault()}
          onDragLeave={() => setDragOver(false)}
          onDrop={onDrop}
        >
          <div className="rounded-xl border-2 border-dashed border-primary bg-primary/5 p-16 text-center">
            <RiUploadCloud2Line size={48} className="mx-auto text-primary mb-3" />
            <p className="text-lg font-medium">Drop your file here</p>
            <p className="text-sm text-muted-foreground mt-1">
              PDF, DOCX, MD, HTML, TXT — up to 25 MB
            </p>
          </div>
        </div>
      )}

      {/* Main content area — scrollable */}
      <div
        className="flex-1 overflow-auto"
        onDragOver={(e) => {
          e.preventDefault();
          setDragOver(true);
        }}
      >
        {docs.length === 0 && !search && !statusFilter && !typeFilter ? (
          /* Empty state — no documents at all */
          <div
            className="flex flex-col items-center justify-center h-full text-center px-6"
            onDrop={onDrop}
          >
            <div
              className={`rounded-xl border-2 border-dashed p-12 text-center transition-colors w-full max-w-md ${
                dragOver ? "border-primary bg-primary/5" : "border-border"
              }`}
              onClick={() => fileRef.current?.click()}
              role="button"
            >
              <RiUploadCloud2Line
                size={36}
                className="mx-auto text-muted-foreground mb-3"
              />
              <p className="text-sm font-medium mb-1">Upload your first document</p>
              <p className="text-xs text-muted-foreground">
                Drag and drop or click to browse
              </p>
              <p className="text-xs text-muted-foreground mt-1">
                PDF, DOCX, MD, HTML, TXT — up to 25 MB
              </p>
            </div>
          </div>
        ) : docs.length === 0 ? (
          /* No results for filters */
          <div className="flex flex-col items-center justify-center h-full text-center px-6">
            <RiSearchLine size={32} className="text-muted-foreground mb-3 opacity-40" />
            <p className="text-sm text-muted-foreground">No documents match your filters</p>
          </div>
        ) : (
          /* Document list */
          <div className="divide-y">
            {docs.map((doc) => (
              <div key={doc.id}>
                {/* Row */}
                <div className="flex items-center gap-3 px-6 py-3 hover:bg-accent/50 transition-colors group">
                  <RiFileTextLine
                    size={16}
                    className="shrink-0 text-muted-foreground"
                  />
                  <Link
                    to={`/documents/${doc.id}`}
                    className="flex-1 min-w-0 text-sm font-medium hover:underline truncate"
                  >
                    {doc.filename}
                  </Link>

                  <StatusDot status={doc.status} />

                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      setExpandedId(expandedId === doc.id ? null : doc.id);
                    }}
                    className="p-1 rounded text-muted-foreground hover:bg-accent transition-colors"
                    title="Details"
                  >
                    <RiInformationLine size={14} />
                  </button>
                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      doDelete(doc.id);
                    }}
                    className="p-1 rounded text-muted-foreground hover:text-destructive hover:bg-destructive/10 transition-colors opacity-0 group-hover:opacity-100"
                    title="Delete"
                  >
                    <RiDeleteBinLine size={14} />
                  </button>
                </div>

                {/* Expanded metadata */}
                {expandedId === doc.id && (
                  <div className="px-6 pb-3 pt-0">
                    <div className="ml-7 rounded-md bg-muted/50 px-4 py-3 grid grid-cols-2 sm:grid-cols-4 gap-x-6 gap-y-2 text-xs">
                      <div>
                        <span className="text-muted-foreground block mb-0.5">Size</span>
                        <span className="font-medium">{formatBytes(doc.size)}</span>
                      </div>
                      <div>
                        <span className="text-muted-foreground block mb-0.5">Type</span>
                        <span className="font-medium uppercase">{doc.sourceType}</span>
                      </div>
                      <div>
                        <span className="text-muted-foreground block mb-0.5">Uploaded</span>
                        <span className="font-medium">{formatRelative(doc.createdAt)}</span>
                      </div>
                      <div>
                        <span className="text-muted-foreground block mb-0.5">Embedding</span>
                        <StatusDot status={doc.embeddingStatus} />
                      </div>
                      <div>
                        <span className="text-muted-foreground block mb-0.5">MIME</span>
                        <span className="font-medium break-all">{doc.mimeType}</span>
                      </div>
                      <div>
                        <span className="text-muted-foreground block mb-0.5">Document ID</span>
                        <span className="font-medium font-mono text-[10px]">
                          {doc.id.slice(0, 8)}...
                        </span>
                      </div>
                      {doc.error && (
                        <div className="col-span-2 sm:col-span-4">
                          <span className="text-muted-foreground block mb-0.5">Error</span>
                          <span className="font-medium text-destructive">{doc.error}</span>
                        </div>
                      )}
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}

        {/* Load more */}
        {pagination.hasMore && (
          <div className="p-4 text-center">
            <Button
              variant="outline"
              size="sm"
              onClick={() => load(pagination.offset + pagination.limit)}
            >
              Load more
            </Button>
          </div>
        )}
      </div>

      {/* Footer */}
      {pagination.total > 0 && (
        <div className="shrink-0 border-t px-6 py-2 text-xs text-muted-foreground">
          {pagination.total} document{pagination.total !== 1 ? "s" : ""}
        </div>
      )}

      {/* Upload progress dialog */}
      <Dialog open={uploadDialog} onOpenChange={setUploadDialog}>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>{uploading ? "Uploading..." : "Upload complete"}</DialogTitle>
          </DialogHeader>
          <div className="mt-3">
            <div className="h-1.5 rounded-full bg-muted overflow-hidden">
              <div
                className="h-full bg-primary transition-all duration-300 rounded-full"
                style={{ width: `${uploadPct}%` }}
              />
            </div>
            <p className="text-xs text-muted-foreground mt-2 text-center">
              {uploading ? `${uploadPct}%` : "Document uploaded successfully"}
            </p>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
