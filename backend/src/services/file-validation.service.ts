export const ALLOWED_SOURCE_TYPES = ["pdf", "docx", "md", "html", "txt"] as const;
export type AllowedSourceType = (typeof ALLOWED_SOURCE_TYPES)[number];

export interface SourceTypeMeta {
  sourceType: AllowedSourceType;
  extension: string; // ".pdf"
  mimeType: string;
}

export interface SourceTypeInfo extends SourceTypeMeta {
  filename: string; // sanitized, path-free name as it should be persisted
}

const BY_EXTENSION: Record<string, SourceTypeMeta> = {
  ".pdf": { sourceType: "pdf", extension: ".pdf", mimeType: "application/pdf" },
  ".docx": {
    sourceType: "docx",
    extension: ".docx",
    mimeType:
      "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
  },
  ".md": { sourceType: "md", extension: ".md", mimeType: "text/markdown" },
  ".html": { sourceType: "html", extension: ".html", mimeType: "text/html" },
  ".txt": { sourceType: "txt", extension: ".txt", mimeType: "text/plain" },
};

const MAGIC_PDF = Buffer.from([0x25, 0x50, 0x44, 0x46, 0x2d]); // "%PDF-"
const MAGIC_ZIP = Buffer.from([0x50, 0x4b, 0x03, 0x04]); // "PK\x03\x04" (docx is a zip)

/** Normalized, server-side file validation. The client's claimed content-type
 * is ignored on purpose: extension allow-list + binary magic sniffing decide
 * what the file really is, so a renamed or spoofed file cannot slip through. */
export class FileValidationService {
  constructor(private readonly maxBytes: number) {}

  validate(filename: string, buffer: Buffer): SourceTypeInfo {
    const safeFilename = sanitizeFilename(filename);
    if (!safeFilename) {
      throw new FileValidationError("INVALID_FILENAME", "filename is empty or unsafe", 400);
    }
    if (safeFilename.length > 255) {
      throw new FileValidationError("INVALID_FILENAME", "filename is too long", 400);
    }

    const extension = extensionOf(safeFilename);
    const info = BY_EXTENSION[extension];
    if (!info) {
      throw new FileValidationError(
        "UNSUPPORTED_FILE_TYPE",
        `unsupported file type: ${extension || "(no extension)"} - allowed: ${ALLOWED_SOURCE_TYPES.join(", ")}`,
        415
      );
    }

    if (buffer.length === 0) {
      throw new FileValidationError("EMPTY_FILE", "uploaded file is empty", 400);
    }
    if (buffer.length > this.maxBytes) {
      throw new FileValidationError(
        "FILE_TOO_LARGE",
        `file is ${buffer.length} bytes; limit is ${this.maxBytes}`,
        413
      );
    }

    // magic bytes must be the file's own prefix; a claim elsewhere is spoofing
    if (info.sourceType === "pdf" && !buffer.subarray(0, MAGIC_PDF.length).equals(MAGIC_PDF)) {
      throw new FileValidationError(
        "FILE_TYPE_MISMATCH",
        "file named .pdf does not contain a PDF signature",
        400
      );
    }
    if (info.sourceType === "docx" && !buffer.subarray(0, MAGIC_ZIP.length).equals(MAGIC_ZIP)) {
      throw new FileValidationError(
        "FILE_TYPE_MISMATCH",
        "file named .docx is not a zip container (docx is an OOXML zip)",
        400
      );
    }
    if (info.sourceType === "md" || info.sourceType === "html" || info.sourceType === "txt") {
      if (buffer.subarray(0, Math.min(buffer.length, 1024)).includes(0)) {
        throw new FileValidationError(
          "FILE_TYPE_MISMATCH",
          "text file contains binary bytes (NUL)",
          400
        );
      }
    }

    return {
      ...info,
      filename: safeFilename,
    };
  }
}

export class FileValidationError extends Error {
  readonly statusCode: number;
  readonly errorCode: string;

  constructor(errorCode: string, message: string, statusCode: number) {
    super(message);
    this.name = "FileValidationError";
    this.errorCode = errorCode;
    this.statusCode = statusCode;
  }
}

function extensionOf(filename: string): string {
  const dot = filename.lastIndexOf(".");
  return dot < 0 ? "" : filename.slice(dot).toLowerCase();
}

/** Collapse any client-supplied path to a bare filename and strip controls.
 * "..", "/", "\" and NUL in a filename are attacker-shaped; never believe it. */
export function sanitizeFilename(filename: string): string {
  const base = filename.split(/[\\/]/).pop() ?? "";
  // strip control chars *after* path splitting, plus a leading dot run
  return base
    .replace(/[\u0000-\u001f\u007f]/g, "")
    .replace(/^\.+/, "")
    .trim();
}