import {
  CreateBucketCommand,
  DeleteObjectCommand,
  HeadBucketCommand,
  HeadObjectCommand,
  PutObjectCommand,
  S3Client,
} from "@aws-sdk/client-s3";
import { NodeHttpHandler } from "@smithy/node-http-handler";

export interface StorageHealth {
  reachable: boolean;
  bucket: string;
  error?: string;
}

/** Minimal surface the app depends on, so tests inject an in-memory fake. */
export interface StorageClient {
  /** Upload an object; creates the bucket lazily on first use. */
  put(key: string, body: Buffer, contentType: string): Promise<void>;
  /** Remove an object (used for compensating actions). */
  delete(key: string): Promise<void>;
  /** Size of an object, or undefined if it does not exist. */
  head(key: string): Promise<{ size: number } | undefined>;
  /** Cheap reachability probe: bucket writable/lookup works? */
  reachable(): Promise<StorageHealth>;
}

export interface StorageConfig {
  endpoint: string;
  region: string;
  bucket: string;
  accessKey: string;
  secretKey: string;
  forcePathStyle: boolean;
}

/** S3-compatible storage backed by the AWS SDK (works against MinIO via
 * forcePathStyle + endpoint). Timeouts are tight so a dead store fails the
 * request fast instead of hanging an upload. */
export class S3StorageClient implements StorageClient {
  private readonly client: S3Client;
  private ensurePromise: Promise<void> | null = null;

  constructor(private readonly config: StorageConfig) {
    this.client = new S3Client({
      region: config.region,
      endpoint: config.endpoint,
      forcePathStyle: config.forcePathStyle,
      credentials: {
        accessKeyId: config.accessKey,
        secretAccessKey: config.secretKey,
      },
      requestHandler: new NodeHttpHandler({
        requestTimeout: 4000,
        connectionTimeout: 4000,
      }),
    });
  }

  private ensureBucket(): Promise<void> {
    this.ensurePromise ??= (async () => {
      try {
        await this.client.send(new HeadBucketCommand({ Bucket: this.config.bucket }));
      } catch {
        await this.client.send(
          new CreateBucketCommand({ Bucket: this.config.bucket })
        );
      }
    })();
    return this.ensurePromise;
  }

  async put(key: string, body: Buffer, contentType: string): Promise<void> {
    await this.ensureBucket();
    await this.client.send(
      new PutObjectCommand({
        Bucket: this.config.bucket,
        Key: key,
        Body: body,
        ContentType: contentType,
      })
    );
  }

  async delete(key: string): Promise<void> {
    await this.client
      .send(new DeleteObjectCommand({ Bucket: this.config.bucket, Key: key }))
      .catch(() => undefined); // delete is best-effort cleanup
  }

  async head(key: string): Promise<{ size: number } | undefined> {
    try {
      const result = await this.client.send(
        new HeadObjectCommand({ Bucket: this.config.bucket, Key: key })
      );
      return { size: result.ContentLength ?? 0 };
    } catch {
      return undefined;
    }
  }

  async reachable(): Promise<StorageHealth> {
    try {
      await this.ensureBucket();
      return { reachable: true, bucket: this.config.bucket };
    } catch (error) {
      return {
        reachable: false,
        bucket: this.config.bucket,
        error: error instanceof Error ? error.message : String(error),
      };
    }
  }
}

/** In-memory storage for tests and day-8 demos without a network. */
export class MemoryStorageClient implements StorageClient {
  private readonly objects = new Map<string, { body: Buffer; size: number }>();
  reachableCalls = 0;

  async put(key: string, body: Buffer): Promise<void> {
    this.objects.set(key, { body, size: body.length });
  }

  async delete(key: string): Promise<void> {
    this.objects.delete(key);
  }

  async head(key: string): Promise<{ size: number } | undefined> {
    const object = this.objects.get(key);
    return object ? { size: object.size } : undefined;
  }

  async reachable(): Promise<StorageHealth> {
    this.reachableCalls += 1;
    return { reachable: true, bucket: "memory" };
  }
}