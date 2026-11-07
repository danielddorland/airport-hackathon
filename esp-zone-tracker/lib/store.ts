import { Redis } from "@upstash/redis";
import { createClient } from "redis";
import type { Config, Reading, Runtime } from "./types";

export type Snapshot = {
  config: Config | null;
  readings: Record<string, Reading>;
  runtime: Runtime | null;
};

export interface Store {
  kind: "redis" | "memory";
  loadAll(): Promise<Snapshot>;
  getConfig(): Promise<Config | null>;
  setConfig(c: Config): Promise<void>;
  putReading(r: Reading): Promise<void>;
  deleteReading(id: string): Promise<void>;
  setRuntime(r: Runtime): Promise<void>;
}

const K_CONFIG = "ezt:config";
const K_READINGS = "ezt:readings";
const K_RUNTIME = "ezt:runtime";

function parse<T>(v: unknown): T | null {
  if (v == null) return null;
  if (typeof v !== "string") return v as T;
  try {
    return JSON.parse(v) as T;
  } catch {
    return null;
  }
}

class RedisStore implements Store {
  kind = "redis" as const;
  constructor(private redis: Redis) {}

  async loadAll(): Promise<Snapshot> {
    const p = this.redis.pipeline();
    p.get(K_CONFIG);
    p.hgetall(K_READINGS);
    p.get(K_RUNTIME);
    const [c, r, rt] = (await p.exec()) as [unknown, Record<string, unknown> | null, unknown];
    const readings: Record<string, Reading> = {};
    for (const [id, v] of Object.entries(r ?? {})) {
      const parsed = parse<Reading>(v);
      if (parsed) readings[id] = parsed;
    }
    return { config: parse<Config>(c), readings, runtime: parse<Runtime>(rt) };
  }
  async getConfig() {
    return parse<Config>(await this.redis.get(K_CONFIG));
  }
  async setConfig(c: Config) {
    await this.redis.set(K_CONFIG, JSON.stringify(c));
  }
  async putReading(r: Reading) {
    await this.redis.hset(K_READINGS, { [r.id]: JSON.stringify(r) });
  }
  async deleteReading(id: string) {
    await this.redis.hdel(K_READINGS, id);
  }
  async setRuntime(r: Runtime) {
    await this.redis.set(K_RUNTIME, JSON.stringify(r));
  }
}

/** In-memory fallback so `npm run dev` works with no database. Not for Vercel. */
type Mem = { config: Config | null; readings: Record<string, Reading>; runtime: Runtime | null };
const g = globalThis as unknown as { __eztMem?: Mem };

class MemoryStore implements Store {
  kind = "memory" as const;
  private m: Mem = (g.__eztMem ??= { config: null, readings: {}, runtime: null });
  async loadAll() {
    return structuredClone({ config: this.m.config, readings: this.m.readings, runtime: this.m.runtime });
  }
  async getConfig() {
    return structuredClone(this.m.config);
  }
  async setConfig(c: Config) {
    this.m.config = structuredClone(c);
  }
  async putReading(r: Reading) {
    this.m.readings[r.id] = { ...r };
  }
  async deleteReading(id: string) {
    delete this.m.readings[id];
  }
  async setRuntime(r: Runtime) {
    this.m.runtime = { ...r };
  }
}

let store: Store | null = null;

/**
 * Finds Upstash REST credentials. Vercel's integration may add a custom prefix
 * (e.g. STORAGE_KV_REST_API_URL), so match any *KV_REST_API_URL / *REDIS_REST_URL.
 */
function findRedisEnv(): { url: string; token: string } | null {
  const env = process.env;
  const pairs: [string, string][] = [
    ["KV_REST_API_URL", "KV_REST_API_TOKEN"],
    ["UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"],
  ];
  for (const key of Object.keys(env)) {
    if (/(KV_REST_API_URL|REDIS_REST_URL)$/.test(key) && key !== "KV_REST_API_URL" && key !== "UPSTASH_REDIS_REST_URL") {
      pairs.push([key, key.replace(/URL$/, "TOKEN")]);
    }
  }
  for (const [u, t] of pairs) {
    if (env[u] && env[t]) return { url: env[u]!, token: env[t]! };
  }
  return null;
}

/** A plain redis:// or rediss:// connection string (Redis Cloud, or Upstash's KV_URL). */
function findRedisUrl(): string | null {
  const env = process.env;
  const keys = ["REDIS_URL", "KV_URL", ...Object.keys(env).filter((k) => /(REDIS_URL|KV_URL)$/.test(k))];
  for (const k of keys) {
    const v = env[k];
    if (v && /^rediss?:\/\//.test(v)) return v;
  }
  return null;
}

/** Store over a normal Redis TCP connection, reused across requests on a warm instance. */
class TcpRedisStore implements Store {
  kind = "redis" as const;
  private client: ReturnType<typeof createClient>;
  private ready: Promise<unknown>;

  constructor(url: string) {
    this.client = createClient({
      url,
      socket: { connectTimeout: 5000, reconnectStrategy: (n: number) => Math.min(100 * (n + 1), 2000) },
    });
    this.client.on("error", (e: unknown) => console.error("redis error:", e));
    this.ready = this.client.connect();
  }

  private async c() {
    await this.ready;
    return this.client;
  }

  async loadAll(): Promise<Snapshot> {
    const c = await this.c();
    const [cfg, r, rt] = await Promise.all([c.get(K_CONFIG), c.hGetAll(K_READINGS), c.get(K_RUNTIME)]);
    const readings: Record<string, Reading> = {};
    for (const [id, v] of Object.entries(r ?? {})) {
      const parsed = parse<Reading>(v);
      if (parsed) readings[id] = parsed;
    }
    return { config: parse<Config>(cfg), readings, runtime: parse<Runtime>(rt) };
  }
  async getConfig() {
    return parse<Config>(await (await this.c()).get(K_CONFIG));
  }
  async setConfig(cfg: Config) {
    await (await this.c()).set(K_CONFIG, JSON.stringify(cfg));
  }
  async putReading(r: Reading) {
    await (await this.c()).hSet(K_READINGS, r.id, JSON.stringify(r));
  }
  async deleteReading(id: string) {
    await (await this.c()).hDel(K_READINGS, id);
  }
  async setRuntime(r: Runtime) {
    await (await this.c()).set(K_RUNTIME, JSON.stringify(r));
  }
}

export function getStore(): Store {
  if (store) return store;
  const creds = findRedisEnv();
  const url = creds?.url;
  const token = creds?.token;
  const tcpUrl = findRedisUrl();
  if (url && token) {
    store = new RedisStore(new Redis({ url, token, automaticDeserialization: false }));
  } else if (tcpUrl) {
    store = new TcpRedisStore(tcpUrl);
  } else {
    if (process.env.VERCEL) {
      console.warn("No Redis env vars found: using in-memory store, which will NOT work reliably on Vercel.");
    }
    store = new MemoryStore();
  }
  return store;
}
