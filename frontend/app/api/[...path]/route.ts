import type { NextRequest } from "next/server";

// Same-origin proxy to the CHECKMATE backend. BACKEND_URL is read on every request
// (not at build time), so one build works against any backend address. Responses are
// streamed through unchanged, which keeps server-sent events live.
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const HOP_BY_HOP = ["connection", "keep-alive", "transfer-encoding", "upgrade", "host", "content-length",
  "proxy-authorization", "te", "trailer"];

function backend(): string {
  return (process.env.BACKEND_URL || "http://localhost:8000").replace(/\/$/, "");
}

async function proxy(req: NextRequest, ctx: { params: { path: string[] } }): Promise<Response> {
  const search = new URL(req.url).search;
  const target = `${backend()}/api/${ctx.params.path.map(encodeURIComponent).join("/")}${search}`;
  const headers = new Headers(req.headers);
  HOP_BY_HOP.forEach((h) => headers.delete(h));
  headers.delete("accept-encoding"); // let the backend send identity-encoded, streamable bodies
  const hasBody = !["GET", "HEAD"].includes(req.method);
  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method: req.method,
      headers,
      body: hasBody ? await req.arrayBuffer() : undefined,
      redirect: "manual",
      cache: "no-store",
      signal: req.signal,
    });
  } catch {
    return Response.json({ detail: "CHECKMATE backend is not reachable." }, { status: 502 });
  }
  const out = new Headers();
  upstream.headers.forEach((value, key) => {
    if (!HOP_BY_HOP.includes(key) && key !== "content-encoding" && key !== "set-cookie") out.set(key, value);
  });
  for (const cookie of upstream.headers.getSetCookie?.() ?? []) out.append("set-cookie", cookie);
  return new Response(upstream.body, { status: upstream.status, statusText: upstream.statusText, headers: out });
}

export const GET = proxy;
export const POST = proxy;
export const PUT = proxy;
export const PATCH = proxy;
export const DELETE = proxy;
