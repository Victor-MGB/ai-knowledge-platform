import { describe, expect, it, vi, beforeEach } from "vitest";

import { OrganizationService } from "../src/services/organization.service.js";
import type { PasswordService } from "../src/services/password.service.js";

vi.mock("../src/database/repositories/organization.repository.js", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/database/repositories/organization.repository.js")>();
  return {
    ...actual,
    findOrganizationById: vi.fn(),
  };
});
vi.mock("../src/database/repositories/member.repository.js", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/database/repositories/member.repository.js")>();
  return {
    ...actual,
    countMembersByOrg: vi.fn(),
    findMemberByOrg: vi.fn(),
    listMembersByOrg: vi.fn(),
    updateMemberRole: vi.fn(),
  };
});
vi.mock("../src/database/repositories/invitation.repository.js", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/database/repositories/invitation.repository.js")>();
  return {
    ...actual,
    findInvitationById: vi.fn(),
    findInvitationByToken: vi.fn(),
    insertInvitation: vi.fn(),
    listInvitationsByOrg: vi.fn(),
    markInvitationAccepted: vi.fn(),
    revokeInvitation: vi.fn(),
  };
});
vi.mock("../src/database/repositories/user.repository.js", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../src/database/repositories/user.repository.js")>();
  return {
    ...actual,
    insertUser: vi.fn(),
  };
});

import {
  findOrganizationById,
} from "../src/database/repositories/organization.repository.js";
import {
  countMembersByOrg,
  findMemberByOrg,
  listMembersByOrg,
  updateMemberRole,
} from "../src/database/repositories/member.repository.js";
import {
  findInvitationById,
  findInvitationByToken,
  insertInvitation,
  listInvitationsByOrg,
  markInvitationAccepted,
  revokeInvitation,
} from "../src/database/repositories/invitation.repository.js";
import { insertUser } from "../src/database/repositories/user.repository.js";

const mock = {
  findOrganizationById: vi.mocked(findOrganizationById),
  countMembersByOrg: vi.mocked(countMembersByOrg),
  findMemberByOrg: vi.mocked(findMemberByOrg),
  listMembersByOrg: vi.mocked(listMembersByOrg),
  updateMemberRole: vi.mocked(updateMemberRole),
  findInvitationById: vi.mocked(findInvitationById),
  findInvitationByToken: vi.mocked(findInvitationByToken),
  insertInvitation: vi.mocked(insertInvitation),
  listInvitationsByOrg: vi.mocked(listInvitationsByOrg),
  markInvitationAccepted: vi.mocked(markInvitationAccepted),
  revokeInvitation: vi.mocked(revokeInvitation),
  insertUser: vi.mocked(insertUser),
};

function fakeDb() {
  const tx = {
    query: vi.fn(async (_text: string, _values?: readonly unknown[]) => ({ rows: [], rowCount: 0 })),
    release: vi.fn(),
  };
  const db = {
    query: vi.fn(async () => ({ rows: [], rowCount: 0 })),
    connect: vi.fn(async () => tx),
  };
  return { db, tx };
}

const passwords: PasswordService = {
  hash: vi.fn(async (plain: string) => `hash:${plain}`),
  verify: vi.fn(),
} as unknown as PasswordService;

function makeService(db: ReturnType<typeof fakeDb>["db"]): OrganizationService {
  return new OrganizationService(db, passwords, 7 * 24 * 60 * 60 * 1000);
}

const MEMBER = {
  id: "m1",
  organization_id: "o1",
  email: "member@example.com",
  role: "member",
  created_at: "2026-01-01T00:00:00Z",
  last_login_at: null,
};

const ORG = { id: "o1", name: "Acme", slug: "acme" };

const INVITATION = {
  id: "i1",
  organization_id: "o1",
  email: "invitee@example.com",
  role: "admin",
  token_hash: "abc",
  status: "pending" as const,
  expires_at: "2099-01-01T00:00:00Z",
  invited_by: "m1",
  created_at: "2026-01-01T00:00:00Z",
  accepted_at: null,
};

beforeEach(() => {
  vi.clearAllMocks();
});

describe("OrganizationService.profile", () => {
  it("returns the org profile with its member count", async () => {
    const { db } = fakeDb();
    mock.findOrganizationById.mockResolvedValue(ORG);
    mock.countMembersByOrg.mockResolvedValue(3);

    const profile = await makeService(db).profile("o1");
    expect(profile).toEqual({ id: "o1", name: "Acme", slug: "acme", memberCount: 3 });
  });

  it("404s when the org does not exist", async () => {
    const { db } = fakeDb();
    mock.findOrganizationById.mockResolvedValue(undefined);
    await expect(makeService(db).profile("nope")).rejects.toMatchObject({
      code: "NOT_FOUND",
      statusCode: 404,
    });
  });
});

describe("OrganizationService.listMembers", () => {
  it("lists the org's members alongside the profile", async () => {
    const { db } = fakeDb();
    mock.listMembersByOrg.mockResolvedValue([MEMBER]);
    mock.findOrganizationById.mockResolvedValue(ORG);
    mock.countMembersByOrg.mockResolvedValue(1);

    const res = await makeService(db).listMembers("o1");
    expect(res.organization.memberCount).toBe(1);
    expect(res.items).toEqual([
      {
        id: "m1",
        email: "member@example.com",
        role: "member",
        createdAt: "2026-01-01T00:00:00Z",
        lastLoginAt: null,
      },
    ]);
  });
});

describe("OrganizationService.updateMemberRole", () => {
  it("rejects reassigning an owner (the owner cannot be demoted)", async () => {
    const { db } = fakeDb();
    mock.findMemberByOrg.mockResolvedValue({ ...MEMBER, role: "owner" });
    await expect(
      makeService(db).updateMemberRole("o1", "m1", "owner", "m1", "admin")
    ).rejects.toMatchObject({ code: "FORBIDDEN", statusCode: 403 });
    expect(mock.updateMemberRole).not.toHaveBeenCalled();
  });

  it("rejects changing one's own role", async () => {
    const { db } = fakeDb();
    mock.findMemberByOrg.mockResolvedValue(MEMBER);
    await expect(
      makeService(db).updateMemberRole("o1", "m1", "admin", "m1", "member")
    ).rejects.toMatchObject({ code: "FORBIDDEN", statusCode: 403 });
  });

  it("rejects granting a role above the caller's rank", async () => {
    const { db } = fakeDb();
    mock.findMemberByOrg.mockResolvedValue(MEMBER);
    await expect(
      makeService(db).updateMemberRole("o1", "adminActor", "admin", "m1", "owner")
    ).rejects.toMatchObject({ code: "FORBIDDEN", statusCode: 403 });
  });

  it("allows a member to promote another member to the same rank", async () => {
    const { db } = fakeDb();
    mock.findMemberByOrg.mockResolvedValue(MEMBER);
    mock.updateMemberRole.mockResolvedValue({ ...MEMBER, role: "member" });
    const view = await makeService(db).updateMemberRole("o1", "m2", "member", "m1", "member");
    expect(view.role).toBe("member");
  });

  it("404s a member not in the org (existence hidden)", async () => {
    const { db } = fakeDb();
    mock.findMemberByOrg.mockResolvedValue(undefined);
    await expect(
      makeService(db).updateMemberRole("o1", "m2", "owner", "m9", "admin")
    ).rejects.toMatchObject({ code: "NOT_FOUND", statusCode: 404 });
  });
});

describe("OrganizationService.createInvitation", () => {
  it("mints an invitation carrying the raw token", async () => {
    const { db } = fakeDb();
    mock.insertInvitation.mockResolvedValue({ ...INVITATION, token_hash: "ignored" });
    const res = await makeService(db).createInvitation("o1", "m1", "owner", {
      email: " Invitee@Example.com ",
      role: "member",
    });
    // email is normalized
    expect(mock.insertInvitation).toHaveBeenCalledWith(
      db,
      expect.objectContaining({ email: "invitee@example.com", role: "member" })
    );
    expect(res.email).toBe("invitee@example.com");
    expect(res.token).toMatch(/^[0-9a-f]{64}$/);
  });

  it("rejects inviting a role above the caller's rank", async () => {
    const { db } = fakeDb();
    await expect(
      makeService(db).createInvitation("o1", "m1", "member", {
        email: "x@example.com",
        role: "admin",
      })
    ).rejects.toMatchObject({ code: "FORBIDDEN", statusCode: 403 });
    expect(mock.insertInvitation).not.toHaveBeenCalled();
  });

  it("rejects an invalid role", async () => {
    const { db } = fakeDb();
    await expect(
      makeService(db).createInvitation("o1", "m1", "owner", {
        email: "x@example.com",
        role: "superuser" as never,
      })
    ).rejects.toMatchObject({ code: "VALIDATION_ERROR", statusCode: 400 });
  });

  it("409s a duplicate pending invitation for the same email", async () => {
    const { db } = fakeDb();
    mock.insertInvitation.mockRejectedValue({ code: "23505" });
    await expect(
      makeService(db).createInvitation("o1", "m1", "owner", {
        email: "x@example.com",
        role: "member",
      })
    ).rejects.toMatchObject({ code: "INVITATION_EXISTS", statusCode: 409 });
  });
});

describe("OrganizationService.revokeInvitation", () => {
  it("revokes a pending invitation", async () => {
    const { db } = fakeDb();
    mock.findInvitationById.mockResolvedValue(INVITATION);
    mock.revokeInvitation.mockResolvedValue(true);
    await expect(makeService(db).revokeInvitation("o1", "i1")).resolves.toBeUndefined();
    expect(mock.revokeInvitation).toHaveBeenCalledWith(db, "o1", "i1");
  });

  it("refuses to revoke an invitation that is not pending", async () => {
    const { db } = fakeDb();
    mock.findInvitationById.mockResolvedValue({ ...INVITATION, status: "accepted" });
    await expect(makeService(db).revokeInvitation("o1", "i1")).rejects.toMatchObject({
      code: "INVITATION_NOT_PENDING",
      statusCode: 409,
    });
  });

  it("404s an invitation not in the org", async () => {
    const { db } = fakeDb();
    mock.findInvitationById.mockResolvedValue(undefined);
    await expect(makeService(db).revokeInvitation("o1", "i1")).rejects.toMatchObject({
      code: "NOT_FOUND",
      statusCode: 404,
    });
  });
});

describe("OrganizationService.acceptInvitation", () => {
  it("creates the invited member and spends the token in one transaction", async () => {
    const { db, tx } = fakeDb();
    mock.findInvitationByToken.mockResolvedValue(INVITATION);
    mock.insertUser.mockResolvedValue({
      id: "new-user",
      organization_id: "o1",
      email: "invitee@example.com",
      role: "admin",
    });
    mock.markInvitationAccepted.mockResolvedValue();
    mock.findOrganizationById.mockResolvedValue(ORG);
    mock.countMembersByOrg.mockResolvedValue(4);

    const res = await makeService(db).acceptInvitation({
      token: "a".repeat(64),
      password: "Sup3rSecret!",
    });

    expect(mock.insertUser).toHaveBeenCalledWith(
      tx,
      "o1",
      "invitee@example.com",
      expect.stringContaining("hash:"),
      "admin"
    );
    expect(mock.markInvitationAccepted).toHaveBeenCalledWith(tx, "i1");
    const texts = (tx.query as ReturnType<typeof vi.fn>).mock.calls.map((c) =>
      (c[0] as string).trimStart().toUpperCase()
    );
    expect(texts).toContain("BEGIN");
    expect(texts).toContain("COMMIT");
    expect(res.user).toMatchObject({ id: "new-user", email: "invitee@example.com", role: "admin" });
    expect(res.organization.memberCount).toBe(4);
    expect(tx.release).toHaveBeenCalled();
  });

  it("rolls back and 409s when the email is already taken", async () => {
    const { db, tx } = fakeDb();
    mock.findInvitationByToken.mockResolvedValue(INVITATION);
    mock.insertUser.mockRejectedValue({ code: "23505" });
    mock.findOrganizationById.mockResolvedValue(ORG);
    mock.countMembersByOrg.mockResolvedValue(1);

    await expect(
      makeService(db).acceptInvitation({ token: "a".repeat(64), password: "Sup3rSecret!" })
    ).rejects.toMatchObject({ code: "EMAIL_TAKEN", statusCode: 409 });
    const texts = (tx.query as ReturnType<typeof vi.fn>).mock.calls.map((c) =>
      (c[0] as string).trimStart().toUpperCase()
    );
    expect(texts).toContain("BEGIN");
    expect(texts).toContain("ROLLBACK");
    expect(texts).not.toContain("COMMIT");
  });

  it("rejects an unknown token", async () => {
    const { db } = fakeDb();
    mock.findInvitationByToken.mockResolvedValue(undefined);
    await expect(
      makeService(db).acceptInvitation({ token: "b".repeat(64), password: "Sup3rSecret!" })
    ).rejects.toMatchObject({ code: "INVITATION_NOT_FOUND", statusCode: 404 });
  });

  it("rejects a spent (already accepted) invitation", async () => {
    const { db } = fakeDb();
    mock.findInvitationByToken.mockResolvedValue({ ...INVITATION, status: "accepted" });
    await expect(
      makeService(db).acceptInvitation({ token: "c".repeat(64), password: "Sup3rSecret!" })
    ).rejects.toMatchObject({ code: "INVITATION_SPENT", statusCode: 409 });
  });

  it("rejects an expired invitation", async () => {
    const { db } = fakeDb();
    mock.findInvitationByToken.mockResolvedValue({
      ...INVITATION,
      expires_at: "2000-01-01T00:00:00Z",
    });
    await expect(
      makeService(db).acceptInvitation({ token: "d".repeat(64), password: "Sup3rSecret!" })
    ).rejects.toMatchObject({ code: "INVITATION_EXPIRED", statusCode: 410 });
  });
});
