const userObject = {
  type: "object",
  required: ["id", "email", "role", "organizationId", "createdAt", "lastLoginAt"],
  properties: {
    id: { type: "string" },
    email: { type: "string" },
    role: { type: "string" },
    organizationId: { type: "string" },
    createdAt: { type: "string" },
    lastLoginAt: { type: ["string", "null"] },
  },
} as const;

const tokenObject = {
  type: "object",
  required: ["accessToken", "refreshToken"],
  properties: {
    accessToken: { type: "string" },
    refreshToken: { type: "string" },
  },
} as const;

const credentialsBody = {
  type: "object",
  additionalProperties: false,
  required: ["email", "password"],
  properties: {
    email: { type: "string", format: "email", maxLength: 320 },
    password: { type: "string", minLength: 8, maxLength: 128 },
    organization: { type: "string", minLength: 1, maxLength: 100 },
    name: { type: "string", minLength: 1, maxLength: 100 },
  },
} as const;

const refreshTokenBody = {
  type: "object",
  additionalProperties: false,
  required: ["refreshToken"],
  properties: {
    refreshToken: { type: "string", minLength: 64, maxLength: 128 },
  },
} as const;

export const registerSchema = {
  body: credentialsBody,
  response: {
    201: {
      type: "object",
      required: ["user", "organization", "tokens"],
      properties: {
        user: userObject,
        organization: {
          type: "object",
          required: ["id", "name", "slug"],
          properties: {
            id: { type: "string" },
            name: { type: "string" },
            slug: { type: "string" },
          },
        },
        tokens: tokenObject,
      },
    },
  },
} as const;

export const loginSchema = {
  body: credentialsBody,
  response: {
    200: {
      type: "object",
      required: ["user", "tokens"],
      properties: { user: userObject, tokens: tokenObject },
    },
  },
} as const;

export const refreshSchema = {
  body: refreshTokenBody,
  response: {
    200: tokenObject,
  },
} as const;

export const logoutSchema = {
  body: refreshTokenBody,
  response: {
    204: { type: "null" },
  },
} as const;

export const meSchema = {
  response: {
    200: userObject,
  },
} as const;