import { useState, useEffect } from "react";
import { sourcesApi, type SourceResponse, type Citation } from "@/lib/api";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { formatDate } from "@/lib/utils";
import { RiFileTextLine, RiPagesLine, RiCloseLine } from "react-icons/ri";

interface SourcesPanelProps {
  citations: Citation[];
  onClose: () => void;
}

export function SourcesPanel({ citations, onClose }: SourcesPanelProps) {
  const [sources, setSources] = useState<
    Record<string, SourceResponse>
  >({});
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const docIds = [
      ...new Set(citations.map((c) => c.documentId).filter(Boolean)),
    ] as string[];
    if (docIds.length === 0) {
      setLoading(false);
      return;
    }
    Promise.all(
      docIds.map(async (id) => {
        try {
          const src = await sourcesApi.get(id);
          return [id, src] as const;
        } catch {
          return null;
        }
      }),
    ).then((results) => {
      const map: Record<string, SourceResponse> = {};
      for (const r of results) {
        if (r) map[r[0]] = r[1];
      }
      setSources(map);
      setLoading(false);
    });
  }, [citations]);

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center justify-between p-4 border-b">
        <h3 className="text-sm font-semibold">Sources</h3>
        <button
          onClick={onClose}
          className="p-1 rounded hover:bg-accent text-muted-foreground"
        >
          <RiCloseLine size={16} />
        </button>
      </div>
      <div className="flex-1 overflow-auto p-4 space-y-4">
        {loading ? (
          <p className="text-sm text-muted-foreground">Loading sources...</p>
        ) : citations.length === 0 ? (
          <p className="text-sm text-muted-foreground">No sources cited</p>
        ) : (
          citations.map((cit) => (
            <Card key={cit.id} className="text-sm">
              <CardHeader className="pb-2">
                <div className="flex items-center justify-between">
                  <Badge variant="outline">[{cit.id}]</Badge>
                  {cit.similarity != null && (
                    <Badge variant="secondary">
                      {(cit.similarity * 100).toFixed(0)}% match
                    </Badge>
                  )}
                </div>
                <CardTitle className="text-sm mt-2">
                  {cit.title ?? sources[cit.documentId ?? ""]?.document.title ?? "Unknown"}
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-2 text-xs text-muted-foreground">
                {cit.section && <p>Section: {cit.section}</p>}
                {cit.page && <p>Page: {cit.page}</p>}
                {cit.documentId && sources[cit.documentId] && (
                  <>
                    <Separator />
                    <div className="flex items-center gap-2">
                      <RiFileTextLine size={12} />
                      <span>{sources[cit.documentId].document.filename}</span>
                    </div>
                    <div className="flex items-center gap-2">
                      <RiPagesLine size={12} />
                      <span>{sources[cit.documentId].pages} pages</span>
                    </div>
                    <p>
                      Uploaded{" "}
                      {formatDate(sources[cit.documentId].document.createdAt)}
                    </p>
                  </>
                )}
              </CardContent>
            </Card>
          ))
        )}
      </div>
    </div>
  );
}
