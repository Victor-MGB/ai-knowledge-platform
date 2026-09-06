import type { DatabaseClient, DatabaseTransaction } from "../postgres.js";

export interface OrganizationRow {
  id: string;
  name: string;
  slug: string;
}

type Executable = DatabaseClient | DatabaseTransaction;

export async function insertOrganization(
  db: Executable,
  name: string,
  slug: string
): Promise<OrganizationRow> {
  const { rows } = await db.query(
    "INSERT INTO organizations (name, slug) VALUES ($1, $2) RETURNING id, name, slug",
    [name, slug]
  );
  return rows[0] as OrganizationRow;
}

export async function findOrganizationBySlug(
  db: Executable,
  slug: string
): Promise<OrganizationRow | undefined> {
  const { rows } = await db.query(
    "SELECT id, name, slug FROM organizations WHERE slug = $1",
    [slug]
  );
  return rows[0] as OrganizationRow | undefined;
}

export async function findOrganizationById(
  db: Executable,
  id: string
): Promise<OrganizationRow | undefined> {
  const { rows } = await db.query(
    "SELECT id, name, slug FROM organizations WHERE id = $1",
    [id]
  );
  return rows[0] as OrganizationRow | undefined;
}