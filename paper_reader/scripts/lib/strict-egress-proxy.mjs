import http from "node:http";
import net from "node:net";


const FORBIDDEN_FORWARD_HEADERS = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "proxy-connection",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);
const MAX_BLOCKED_CONNECTS = 256;
const MAX_BLOCKED_AFTER_SEAL = 256;
const MAX_PROXY_VIOLATIONS = 256;
const DEFAULT_PROXY_RESPONSE_MAX_BYTES = 8 * 1024 * 1024;
const PROXY_RESPONSE_HARD_MAX_BYTES = 64 * 1024 * 1024;


function defaultDial({host, port}) {
  return net.connect({host, port});
}


function rejectHttp(response, statusCode = 403) {
  if (response.headersSent || response.destroyed) {
    response.destroy();
    return;
  }
  response.writeHead(statusCode, {
    "content-length": "0",
    connection: "close",
  });
  response.end();
}


function rejectConnect(socket, statusCode = 403) {
  if (!socket.destroyed) {
    const phrase = statusCode === 403 ? "Forbidden" : "Bad Gateway";
    socket.end(
      `HTTP/1.1 ${statusCode} ${phrase}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n`,
    );
  }
}


function parseConnectAuthority(value) {
  if (typeof value !== "string" || value.length === 0 || value.length > 1024) {
    return null;
  }
  const ipv6Match = value.match(/^\[([^\]]+)\]:(\d{1,5})$/);
  const hostnameMatch = value.match(/^([^:\[\]@/?#]+):(\d{1,5})$/);
  const match = ipv6Match || hostnameMatch;
  if (!match) {
    return null;
  }
  const hostname = match[1].replace(/\.$/, "").toLowerCase();
  const port = Number.parseInt(match[2], 10);
  if (!hostname || !Number.isSafeInteger(port) || port < 1 || port > 65535) {
    return null;
  }
  return {hostname, port};
}


function serializeForwardHeaders(headers, expectedHost) {
  const forwarded = {};
  for (const [rawName, rawValue] of Object.entries(headers)) {
    const name = rawName.toLowerCase();
    if (FORBIDDEN_FORWARD_HEADERS.has(name) || rawValue === undefined) {
      continue;
    }
    const values = Array.isArray(rawValue) ? rawValue : [rawValue];
    for (const value of values) {
      const text = String(value);
      if (/\r|\n/.test(text)) {
        throw new Error("invalid_proxy_header");
      }
      if (forwarded[rawName] === undefined) {
        forwarded[rawName] = text;
      } else if (Array.isArray(forwarded[rawName])) {
        forwarded[rawName].push(text);
      } else {
        forwarded[rawName] = [forwarded[rawName], text];
      }
    }
  }
  forwarded.host = expectedHost;
  forwarded.connection = "close";
  return forwarded;
}


function requestHasBody(headers) {
  const transferEncoding = headers["transfer-encoding"];
  if (transferEncoding !== undefined) {
    return true;
  }
  const contentLength = headers["content-length"];
  if (contentLength === undefined) {
    return false;
  }
  const values = Array.isArray(contentLength) ? contentLength : [contentLength];
  return values.some((value) => !/^0+$/.test(String(value).trim()));
}


export async function createStrictEgressProxy({
  policy,
  dial = defaultDial,
  connectTimeoutMs = 10_000,
  maxResponseBytes = DEFAULT_PROXY_RESPONSE_MAX_BYTES,
  onViolation = () => {},
} = {}) {
  if (
    !policy ||
    typeof policy.authorizationForAuthority !== "function" ||
    typeof policy.isProxyUrlAuthorized !== "function"
  ) {
    throw new TypeError("policy is required");
  }
  if (typeof dial !== "function") {
    throw new TypeError("dial must be a function");
  }
  if (
    !Number.isSafeInteger(maxResponseBytes) ||
    maxResponseBytes < 1 ||
    maxResponseBytes > PROXY_RESPONSE_HARD_MAX_BYTES
  ) {
    throw new TypeError("invalid proxy response byte limit");
  }
  const sockets = new Set();
  const violations = [];
  const blockedConnects = [];
  const blockedAfterSeal = [];
  let sealed = false;
  let violationLimitRecorded = false;
  let postSealLimitRecorded = false;
  const recordViolation = (reason) => {
    if (violations.length < MAX_PROXY_VIOLATIONS) {
      violations.push(reason);
      onViolation(reason);
      return;
    }
    if (!violationLimitRecorded) {
      violationLimitRecorded = true;
      violations[MAX_PROXY_VIOLATIONS - 1] = "proxy_violation_limit";
      onViolation("proxy_violation_limit");
    }
  };
  const recordBlockedAfterSeal = (reason) => {
    if (blockedAfterSeal.length < MAX_BLOCKED_AFTER_SEAL) {
      blockedAfterSeal.push(reason);
      return;
    }
    if (!postSealLimitRecorded) {
      postSealLimitRecorded = true;
      recordViolation("proxy_post_seal_limit");
    }
  };
  const sealProxy = () => {
    if (sealed) return;
    sealed = true;
    for (const socket of sockets) {
      socket.destroy();
    }
  };

  const server = http.createServer((request, response) => {
    if (sealed) {
      recordBlockedAfterSeal("proxy_request_after_seal");
      rejectHttp(response);
      return;
    }
    let parsed;
    try {
      parsed = new URL(request.url);
    } catch {
      recordViolation("proxy_invalid_http_url");
      rejectHttp(response);
      return;
    }
    const exactUrl = parsed.href;
    const expectedHost = parsed.host;
    const method = String(request.method || "").toUpperCase();
    if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
      recordViolation("proxy_unsafe_method");
      rejectHttp(response);
      return;
    }
    if (requestHasBody(request.headers)) {
      recordViolation("proxy_request_body");
      rejectHttp(response);
      return;
    }
    if (
      !policy.isProxyUrlAuthorized(exactUrl) ||
      typeof request.headers.host !== "string" ||
      request.headers.host.toLowerCase() !== expectedHost.toLowerCase()
    ) {
      recordViolation("proxy_unauthorized_http_url");
      rejectHttp(response);
      return;
    }
    let hostname = parsed.hostname.toLowerCase();
    if (hostname.startsWith("[") && hostname.endsWith("]")) {
      hostname = hostname.slice(1, -1);
    }
    const port = parsed.port
      ? Number.parseInt(parsed.port, 10)
      : (parsed.protocol === "https:" ? 443 : 80);
    const authorization = policy.authorizationForAuthority("http:", hostname, port);
    if (!authorization || parsed.protocol !== "http:") {
      recordViolation("proxy_unauthorized_http_authority");
      rejectHttp(response);
      return;
    }

    let upstream;
    let upstreamRequest;
    const upstreamAgent = new http.Agent({keepAlive: false});
    upstreamAgent.createConnection = () => {
      upstream = dial({host: authorization.address, port: authorization.port});
      sockets.add(upstream);
      upstream.once("close", () => sockets.delete(upstream));
      return upstream;
    };
    try {
      const headers = serializeForwardHeaders(request.headers, expectedHost);
      const requestTarget = `${parsed.pathname || "/"}${parsed.search}`;
      upstreamRequest = http.request({
        host: authorization.address,
        port: authorization.port,
        method: request.method || "GET",
        path: requestTarget,
        headers,
        agent: upstreamAgent,
      }, (upstreamResponse) => {
        const responseHeaders = {};
        for (const [name, value] of Object.entries(upstreamResponse.headers)) {
          if (!FORBIDDEN_FORWARD_HEADERS.has(name.toLowerCase()) && value !== undefined) {
            responseHeaders[name] = value;
          }
        }
        responseHeaders.connection = "close";
        response.writeHead(
          upstreamResponse.statusCode || 502,
          upstreamResponse.statusMessage || undefined,
          responseHeaders,
        );
        let responseBytes = 0;
        let responseLimited = false;
        upstreamResponse.on("data", (chunk) => {
          if (responseLimited) return;
          const nextBytes = responseBytes + chunk.length;
          if (!Number.isSafeInteger(nextBytes) || nextBytes > maxResponseBytes) {
            responseLimited = true;
            recordViolation("proxy_response_resource_limit");
            upstreamResponse.destroy(new Error("proxy_response_resource_limit"));
            response.destroy();
            sealProxy();
            return;
          }
          responseBytes = nextBytes;
          if (!response.write(chunk)) {
            upstreamResponse.pause();
            response.once("drain", () => upstreamResponse.resume());
          }
        });
        upstreamResponse.once("end", () => {
          if (!responseLimited && !response.destroyed) {
            response.end();
          }
        });
        upstreamResponse.once("error", () => {
          if (!response.destroyed) {
            response.destroy();
          }
        });
      });
    } catch {
      upstreamAgent.destroy();
      rejectHttp(response, 502);
      return;
    }
    upstreamRequest.setTimeout(connectTimeoutMs, () => {
      upstreamRequest.destroy(new Error("proxy_connect_timeout"));
    });
    upstreamRequest.once("error", () => {
      upstreamAgent.destroy();
      if (!response.headersSent) {
        rejectHttp(response, 502);
      } else {
        response.destroy();
      }
    });
    upstreamRequest.once("close", () => upstreamAgent.destroy());
    request.pipe(upstreamRequest);
  });

  server.on("connection", (socket) => {
    sockets.add(socket);
    socket.once("close", () => sockets.delete(socket));
    // A CONNECT peer may reset while the opposite pipe still has buffered
    // bytes. Contain that transport error locally; policy/audit decisions are
    // made from the request and proxy records, never from an uncaught socket
    // exception that would break the one-JSON CLI contract.
    socket.on("error", () => socket.destroy());
    if (sealed) {
      recordBlockedAfterSeal("proxy_connection_after_seal");
      socket.destroy();
    }
  });

  server.on("connect", (request, clientSocket, head) => {
    sockets.add(clientSocket);
    clientSocket.once("close", () => sockets.delete(clientSocket));
    if (sealed) {
      recordBlockedAfterSeal("proxy_connect_after_seal");
      rejectConnect(clientSocket);
      return;
    }
    const authority = parseConnectAuthority(request.url);
    const authorization = authority
      ? policy.authorizationForAuthority("https:", authority.hostname, authority.port)
      : null;
    if (!authority || !authorization) {
      if (!authority) {
        recordViolation("proxy_unauthorized_connect");
      } else if (blockedConnects.length >= MAX_BLOCKED_CONNECTS) {
        if (!violations.includes("proxy_blocked_connect_limit")) {
          recordViolation("proxy_blocked_connect_limit");
        }
      } else {
        blockedConnects.push(Object.freeze({
          protocol: "https:",
          hostname: authority.hostname,
          port: authority.port,
        }));
      }
      rejectConnect(clientSocket);
      return;
    }
    let upstream;
    try {
      upstream = dial({host: authorization.address, port: authorization.port});
    } catch {
      rejectConnect(clientSocket, 502);
      return;
    }
    sockets.add(upstream);
    let tunnelEstablished = false;
    const timer = setTimeout(() => upstream.destroy(new Error("proxy_connect_timeout")), connectTimeoutMs);
    clientSocket.once("close", () => {
      clearTimeout(timer);
      if (!upstream.destroyed) {
        upstream.destroy();
      }
    });
    upstream.once("connect", () => {
      tunnelEstablished = true;
      clearTimeout(timer);
      clientSocket.write("HTTP/1.1 200 Connection Established\r\n\r\n");
      if (head.length) {
        upstream.write(head);
      }
      clientSocket.pipe(upstream);
      upstream.pipe(clientSocket);
    });
    upstream.once("error", () => {
      clearTimeout(timer);
      if (tunnelEstablished) {
        clientSocket.destroy();
      } else {
        rejectConnect(clientSocket, 502);
      }
    });
    upstream.once("close", () => {
      clearTimeout(timer);
      sockets.delete(upstream);
      if (tunnelEstablished && !clientSocket.destroyed) {
        clientSocket.destroy();
      }
    });
  });

  server.on("clientError", (_error, socket) => {
    if (sealed) {
      recordBlockedAfterSeal("proxy_client_error_after_seal");
    } else {
      recordViolation("proxy_client_error");
    }
    socket.destroy();
  });

  await new Promise((resolve, reject) => {
    const onError = (error) => {
      server.removeListener("listening", onListening);
      reject(error);
    };
    const onListening = () => {
      server.removeListener("error", onError);
      resolve();
    };
    server.once("error", onError);
    server.once("listening", onListening);
    server.listen(0, "127.0.0.1");
  });
  const address = server.address();
  if (!address || typeof address === "string") {
    throw new Error("proxy_listen_failed");
  }

  return {
    host: "127.0.0.1",
    port: address.port,
    violations,
    blockedConnects,
    blockedAfterSeal,
    seal() {
      sealProxy();
    },
    async close() {
      sealProxy();
      await new Promise((resolve) => server.close(resolve));
    },
  };
}
