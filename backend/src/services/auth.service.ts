import { randomBytes } from "node:crypto";

import { AppError } from "../utils/errors.js";
import type { DatabaseClient, DatabaseTransaction } from "../database/postgres.js";
import {
  findOrganizationBySlug,
  insertOrganization,
} from "../database/repositories/organization.repository.js";
import {
  findUserById,
  findUsersByEmail,
  insertUser,
  touchLastLogin,
  type UserRow,
} from "../database/repositories/user.repository.js";
import {
  findValidRefreshToken,
  insertRefreshToken,
  revokeRefreshToken,
} from "../database/repositories/refresh-token.repository.js";
import { PasswordService } from "./password.service.js";
import { RefreshTokenIssuer, hashToken } from "./token.service.js";
import { slugify } from "../utils/slug.js";

export interface AuthUserView {
  id: string;
  email: string;
  role: string;
  organizationId: string;
  createdAt: string;
  lastLoginAt: string | null;
}

export interface TokenPair {
  accessToken: string;
  refreshToken: string;
}

export interface RegisterInput {
  email: string;
  password: string;
  name?: string;
}

export interface LoginInput {
  email: string;
  password: string;
  organization?: string; // slug, to disambiguate same-email accounts across tenants
}

export interface RegisterResult {
  user: AuthUserView;
  organization: { id: string; name: string; slug: string };
  tokens: TokenPair;
}

const INVALID_CREDENTIALS = () =>
  new AppError("INVALID_CREDENTIALS", "email or password is incorrect", 401);

function isUniqueViolation(error: unknown): boolean {
  return (
    typeof error === "object" &&
    error !== null &&
    (error as { code?: string }).code === "23505"
  );
}

function toView(user: UserRow): AuthUserView {
  return {
    id: user.id,
    email: user.email,
    role: user.role,
    organizationId: user.organization_id,
    createdAt: user.created_at,
    lastLoginAt: user.last_login_at,
  };
}

export class AuthService {
  private readonly tokens = new RefreshTokenIssuer();

  constructor(
    private readonly db: DatabaseClient,
    private readonly passwords: PasswordService,
    private readonly issueAccess: (user: UserRow) => string,
    private readonly refreshTtlMs: number
  ) {}

  /** Create a tenant and its first (owner) account in one transaction. */
  async register(input: RegisterInput): Promise<RegisterResult> {
    const connection = await this.db.connect?.();
    if (!connection) {
      throw new AppError("DATASTORE_UNAVAILABLE", "database connection unavailable", 503);
    }
    // widen from optional to concrete so narrowing survives into catch
    const transaction: DatabaseTransaction = connection;
    try {
      await transaction.query("BEGIN");
      const name = input.name?.trim() || input.email.split("@")[0] || "Team";
      const slug = `${slugify(name)}-${randomBytesHex(3)}`;
      const organization = await insertOrganization(transaction, name, slug);
      const passwordHash = await this.passwords.hash(input.password);
      const user = await insertUser(transaction, organization.id, input.email, passwordHash, "owner");
      await transaction.query("COMMIT");

      const tokens = await this.issueTokens(user);
      return {
        user: toView(user),
        organization: {
          id: organization.id,
          name: organization.name,
          slug: organization.slug,
        },
        tokens,
      };
    } catch (error) {
      await transaction.query("ROLLBACK").catch(() => undefined);
      if (isUniqueViolation(error)) {
        throw new AppError(
          "EMAIL_TAKEN",
          "an account with this email already exists in this organization",
          409
        );
      }
      throw error;
    } finally {
      transaction.release();
    }
  }

  async login(input: LoginInput): Promise<{ user: AuthUserView; tokens: TokenPair }> {
    const candidates = await findUsersByEmail(this.db, input.email);
    let user: UserRow;

    if (input.organization) {
      const org = await findOrganizationBySlug(this.db, input.organization);
      const match = candidates.find((candidate) => candidate.organization_id === org?.id);
      if (!org || !match) throw INVALID_CREDENTIALS();
      user = match;
    } else if (candidates.length === 1) {
      const only = candidates[0];
      if (!only) throw INVALID_CREDENTIALS();
      user = only;
    } else if (candidates.length === 0) {
      throw INVALID_CREDENTIALS();
    } else {
      throw new AppError(
        "MULTIPLE_ACCOUNTS",
        "this email exists in several organizations - pass your organization slug to log in",
        422
      );
    }

    // Generic error whether the email is unknown or the password is wrong,
    // so an attacker cannot enumerate accounts.
    if (!(await this.passwords.verify(input.password, user.password_hash))) {
      throw INVALID_CREDENTIALS();
    }

    await touchLastLogin(this.db, user.id);
    return { user: toView(user), tokens: await this.issueTokens(user) };
  }

  /** Rotate: the presented token is revoked and a fresh pair issued. Replay of
   * an already-rotated token fails, bounding the blast radius of a leak. */
  async refresh(refreshToken: string): Promise<TokenPair> {
    const hash = hashToken(refreshToken);
    const row = await findValidRefreshToken(this.db, hash);
    if (!row) {
      throw new AppError("INVALID_REFRESH_TOKEN", "refresh token is invalid or expired", 401);
    }
    const user = await this.findUserByIdOrThrow(row.user_id);
    await revokeRefreshToken(this.db, hash);
    return this.issueTokens(user);
  }

  async logout(refreshToken: string): Promise<void> {
    await revokeRefreshToken(this.db, hashToken(refreshToken));
  }

  async profile(userId: string): Promise<AuthUserView> {
    const user = await this.findUserByIdOrThrow(userId);
    return toView(user);
  }

  private async findUserByIdOrThrow(userId: string): Promise<UserRow> {
    const user = await findUserById(this.db, userId);
    if (!user) {
      throw new AppError("INVALID_REFRESH_TOKEN", "refresh token is invalid or expired", 401);
    }
    return user;
  }

  private async issueTokens(user: UserRow): Promise<TokenPair> {
    const accessToken = this.issueAccess(user);
    const refresh = this.tokens.issue();
    await insertRefreshToken(
      this.db,
      user.id,
      user.organization_id,
      refresh.tokenHash,
      new Date(Date.now() + this.refreshTtlMs)
    );
    return { accessToken, refreshToken: refresh.token };
  }
}

function randomBytesHex(bytes: number): string {
  return randomBytes(bytes).toString("hex");
}