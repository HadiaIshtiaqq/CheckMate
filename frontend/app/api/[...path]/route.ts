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
  // Forward only the last X-Forwarded-For hop (the one Next.js added from the socket).
  // Earlier entries are client-supplied and could be spoofed to dodge rate limits.
  const xff = req.headers.get("x-forwarded-for");
  const lastHop = xff?.split(",").map((s) => s.trim()).filter(Boolean).pop();
  // On a host whose edge sets a trusted client-IP header (e.g. Cloudflare's True-Client-IP),
  // CLIENT_IP_HEADER names it so rate limits see each visitor rather than the host's proxy.
  const ipHeader = process.env.CLIENT_IP_HEADER?.trim().toLowerCase();
  const edgeIp = ipHeader ? req.headers.get(ipHeader)?.split(",")[0].trim() : undefined;
  const ip = edgeIp || lastHop;
  if (ip) headers.set("x-forwarded-for", ip); else headers.delete("x-forwarded-for");
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
