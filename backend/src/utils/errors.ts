/** Application error with an HTTP-friendly shape.

Routes can throw `new AppError("NOT_FOUND", "...", 404)` (or return it) and the
central error handler will render the matching JSON envelope instead of a bare
500. Unknown routes and validation failures get handled separately.
 */

export interface ErrorDetail {
  code: string;
  message: string;
  statusCode?: number;
  details?: unknown;
}

export class AppError extends Error {
  readonly statusCode: number;
  readonly code: string;
  readonly details: unknown;

  constructor(code: string, message: string, statusCode = 500, details?: unknown) {
    super(message);
    this.name = "AppError";
    this.code = code;
    this.statusCode = statusCode;
    this.details = details;
  }
}

export const isAppError = (error: unknown): error is AppError =>
  error instanceof AppError;