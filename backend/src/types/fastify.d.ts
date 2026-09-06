import type { Config } from "../config/config.js";
import type { DatabaseClient } from "../database/postgres.js";
import type { AuthService } from "../services/auth.service.js";
import type { DocumentsService } from "../services/documents.service.js";
import type { RagService } from "../services/rag.service.js";
import type { SearchService } from "../services/search.service.js";
import type { SourcesService } from "../services/sources.service.js";
import type { ConversationsService } from "../services/conversations.service.js";
import type { OrganizationService } from "../services/organization.service.js";
import type { HealthService } from "../services/health.service.js";
import type { StorageClient } from "../services/storage.service.js";
import type { AccessTokenPayload } from "../services/token.service.js";

declare module "fastify" {
  interface FastifyInstance {
    config: Config;
    db: DatabaseClient;
    healthService: HealthService;
    authService: AuthService;
    storageService: StorageClient;
    documentsService: DocumentsService;
    searchService: SearchService;
    ragService: RagService;
    sourcesService: SourcesService;
    conversationsService: ConversationsService;
    organizationService: OrganizationService;
  }
}

declare module "@fastify/jwt" {
  interface FastifyJWT {
    payload: AccessTokenPayload;
    user: AccessTokenPayload;
  }
}