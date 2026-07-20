import assert from "node:assert/strict";
import http from "node:http";
import net from "node:net";
import test from "node:test";

import { PublicNetworkPolicy } from "../../scripts/lib/secondary-network-policy.mjs";
import { createStrictEgressProxy } from "../../scripts/lib/strict-egress-proxy.mjs";


function listen(server) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => {
      server.removeListener("error", reject);
      resolve(server.address());
    });
  });
}


function closeServer(server) {
  return new Promise((resolve) => server.close(resolve));
}


function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}


function proxyRequest({ proxyPort, absoluteUrl, method = "GET", body = null }) {
  return new Promise((resolve, reject) => {
    const request = http.request({
      host: "127.0.0.1",
      port: proxyPort,
      method,
      path: absoluteUrl,
      headers: {
        host: new URL(absoluteUrl).host,
        ...(body === null ? {} : {"content-length": Buffer.byteLength(body)}),
      },
    }, (response) => {
      const chunks = [];
      response.on("data", (chunk) => chunks.push(chunk));
      response.on("end", () => resolve({
        statusCode: response.statusCode,
        body: Buffer.concat(chunks).toString("utf8"),
      }));
    });
    request.once("error", reject);
    request.end(body);
  });
}


function proxyRequestOutcome({proxyPort, absoluteUrl}) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("proxy response test timeout")), 2000);
    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(value);
    };
    const request = http.request({
      host: "127.0.0.1",
      port: proxyPort,
      method: "GET",
      path: absoluteUrl,
      headers: {host: new URL(absoluteUrl).host},
    }, (response) => {
      let bytes = 0;
      response.on("data", (chunk) => {
        bytes += chunk.length;
      });
      response.once("end", () => finish({complete: true, bytes}));
      response.once("aborted", () => finish({complete: false, bytes}));
      response.once("error", () => finish({complete: false, bytes}));
    });
    request.once("error", (error) => {
      if (settled) return;
      if (error?.code === "ECONNRESET") {
        finish({complete: false, bytes: 0});
        return;
      }
      clearTimeout(timer);
      reject(error);
    });
    request.end();
  });
}


test("forwards only an exact authorized HTTP URL to its pinned public address", async () => {
  let receivedRequest = "";
  const backend = net.createServer((socket) => {
    socket.once("data", (chunk) => {
      receivedRequest = chunk.toString("utf8");
      socket.end("HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok");
    });
  });
  const backendAddress = await listen(backend);
  const dials = [];
  const policy = new PublicNetworkPolicy({resolver: async () => ["93.184.216.34"]});
  await policy.authorizeUrl("http://public.example:8080/path?x=1");
  await policy.authorizeUrl("http://public.example:8080/second-resource");
  const proxy = await createStrictEgressProxy({
    policy,
    dial: ({host, port}) => {
      dials.push({host, port});
      return net.connect(backendAddress.port, "127.0.0.1");
    },
  });

  try {
    const accepted = await proxyRequest({
      proxyPort: proxy.port,
      absoluteUrl: "http://public.example:8080/path?x=1",
    });
    const rejected = await proxyRequest({
      proxyPort: proxy.port,
      absoluteUrl: "http://public.example:8080/not-authorized",
    });

    assert.deepEqual(accepted, {statusCode: 200, body: "ok"});
    assert.equal(rejected.statusCode, 403);
    assert.deepEqual(dials, [{host: "93.184.216.34", port: 8080}]);
    assert.match(receivedRequest, /^GET \/path\?x=1 HTTP\/1\.1\r\n/);
  } finally {
    await proxy.close();
    await closeServer(backend);
  }
});


test("opens CONNECT only for an authorized authority and dials the pinned IP", async () => {
  const backend = net.createServer((socket) => socket.pipe(socket));
  const backendAddress = await listen(backend);
  const dials = [];
  const policy = new PublicNetworkPolicy({resolver: async () => ["93.184.216.34"]});
  await policy.authorizeUrl("https://public.example/article");
  const proxy = await createStrictEgressProxy({
    policy,
    dial: ({host, port}) => {
      dials.push({host, port});
      return net.connect(backendAddress.port, "127.0.0.1");
    },
  });

  try {
    const socket = net.connect(proxy.port, "127.0.0.1");
    socket.write("CONNECT public.example:443 HTTP/1.1\r\nHost: public.example:443\r\n\r\n");
    const response = await new Promise((resolve, reject) => {
      let received = Buffer.alloc(0);
      const timer = setTimeout(() => reject(new Error("connect test timeout")), 2000);
      socket.on("data", (chunk) => {
        received = Buffer.concat([received, chunk]);
        if (received.includes(Buffer.from("\r\n\r\n"))) {
          clearTimeout(timer);
          resolve(received.toString("utf8"));
        }
      });
      socket.once("error", reject);
    });
    socket.destroy();

    assert.match(response, /^HTTP\/1\.1 200 Connection Established/);
    assert.deepEqual(dials, [{host: "93.184.216.34", port: 443}]);
  } finally {
    await proxy.close();
    await closeServer(backend);
  }
});


test("contains a CONNECT peer reset while upstream data is still arriving", async () => {
  let backendSocket;
  const backend = net.createServer((socket) => {
    backendSocket = socket;
    socket.on("error", () => {});
  });
  const backendAddress = await listen(backend);
  const policy = new PublicNetworkPolicy({resolver: async () => ["93.184.216.34"]});
  await policy.authorizeUrl("https://public.example/article");
  const proxy = await createStrictEgressProxy({
    policy,
    dial: () => net.connect(backendAddress.port, "127.0.0.1"),
  });

  try {
    const socket = net.connect(proxy.port, "127.0.0.1");
    socket.write("CONNECT public.example:443 HTTP/1.1\r\nHost: public.example:443\r\n\r\n");
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("connect reset test timeout")), 2000);
      socket.on("data", (chunk) => {
        if (chunk.includes(Buffer.from("\r\n\r\n"))) {
          clearTimeout(timer);
          resolve();
        }
      });
      socket.once("error", reject);
    });
    assert.ok(backendSocket);
    socket.resetAndDestroy();
    for (let index = 0; index < 32; index += 1) {
      backendSocket.write(Buffer.alloc(64 * 1024, index));
    }
    await sleep(100);

    assert.equal(socket.destroyed, true);
  } finally {
    await proxy.close();
    await closeServer(backend);
  }
});


test("rejects an unauthorized CONNECT before dialing", async () => {
  let dialed = false;
  const policy = new PublicNetworkPolicy({resolver: async () => ["93.184.216.34"]});
  const proxy = await createStrictEgressProxy({
    policy,
    dial: () => {
      dialed = true;
      throw new Error("must not dial");
    },
  });

  try {
    const socket = net.connect(proxy.port, "127.0.0.1");
    socket.write("CONNECT private.example:443 HTTP/1.1\r\nHost: private.example:443\r\n\r\n");
    const response = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("reject test timeout")), 2000);
      socket.once("data", (chunk) => {
        clearTimeout(timer);
        resolve(chunk.toString("utf8"));
      });
      socket.once("error", reject);
    });
    socket.destroy();

    assert.match(response, /^HTTP\/1\.1 403 Forbidden/);
    assert.equal(dialed, false);
    assert.deepEqual(proxy.blockedConnects, [
      {protocol: "https:", hostname: "private.example", port: 443},
    ]);
    assert.equal(proxy.violations.includes("proxy_unauthorized_connect"), false);
  } finally {
    await proxy.close();
  }
});


test("does not treat a cleartext authority authorization as HTTPS CONNECT approval", async () => {
  let dialed = false;
  const policy = new PublicNetworkPolicy({resolver: async () => ["93.184.216.34"]});
  await policy.authorizeUrl("http://public.example:443/plaintext");
  const proxy = await createStrictEgressProxy({
    policy,
    dial: () => {
      dialed = true;
      throw new Error("must not dial");
    },
  });

  try {
    const socket = net.connect(proxy.port, "127.0.0.1");
    socket.write("CONNECT public.example:443 HTTP/1.1\r\nHost: public.example:443\r\n\r\n");
    const response = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("scheme test timeout")), 2000);
      socket.once("data", (chunk) => {
        clearTimeout(timer);
        resolve(chunk.toString("utf8"));
      });
      socket.once("error", reject);
    });
    socket.destroy();

    assert.match(response, /^HTTP\/1\.1 403 Forbidden/);
    assert.equal(dialed, false);
    assert.deepEqual(proxy.blockedConnects, [
      {protocol: "https:", hostname: "public.example", port: 443},
    ]);
  } finally {
    await proxy.close();
  }
});


test("closes a malformed proxy client instead of leaking the socket", async () => {
  const policy = new PublicNetworkPolicy({resolver: async () => ["93.184.216.34"]});
  const proxy = await createStrictEgressProxy({policy});

  try {
    const socket = net.connect(proxy.port, "127.0.0.1");
    const closed = new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("malformed client remained open")), 500);
      socket.once("close", () => {
        clearTimeout(timer);
        resolve();
      });
      socket.once("error", () => {});
    });
    socket.write("NOT HTTP\r\n\r\n");
    await closed;

    assert.ok(proxy.violations.includes("proxy_client_error"));
  } finally {
    await proxy.close();
  }
});


test("rejects an unsafe cleartext HTTP method before dialing", async () => {
  let dialed = false;
  const policy = new PublicNetworkPolicy({resolver: async () => ["93.184.216.34"]});
  await policy.authorizeUrl("http://public.example/telemetry");
  const proxy = await createStrictEgressProxy({
    policy,
    dial: () => {
      dialed = true;
      throw new Error("must not dial");
    },
  });

  try {
    const response = await proxyRequest({
      proxyPort: proxy.port,
      absoluteUrl: "http://public.example/telemetry",
      method: "POST",
    });
    assert.equal(response.statusCode, 403);
    assert.equal(dialed, false);
    assert.ok(proxy.violations.includes("proxy_unsafe_method"));
  } finally {
    await proxy.close();
  }
});


test("rejects a cleartext request body before dialing", async () => {
  let dialed = false;
  const policy = new PublicNetworkPolicy({resolver: async () => ["93.184.216.34"]});
  await policy.authorizeUrl("http://public.example/options");
  const proxy = await createStrictEgressProxy({
    policy,
    dial: () => {
      dialed = true;
      throw new Error("must not dial");
    },
  });

  try {
    const response = await proxyRequest({
      proxyPort: proxy.port,
      absoluteUrl: "http://public.example/options",
      method: "OPTIONS",
      body: "side-effect",
    });
    assert.equal(response.statusCode, 403);
    assert.equal(dialed, false);
    assert.ok(proxy.violations.includes("proxy_request_body"));
  } finally {
    await proxy.close();
  }
});


test("terminates an unbounded cleartext response at the proxy byte limit", async () => {
  const backend = http.createServer((_request, response) => {
    response.writeHead(200, {"content-type": "text/html", connection: "close"});
    for (let index = 0; index < 8; index += 1) {
      response.write(Buffer.alloc(32, index));
    }
    response.end();
  });
  const backendAddress = await listen(backend);
  const policy = new PublicNetworkPolicy({resolver: async () => ["93.184.216.34"]});
  await policy.authorizeUrl("http://public.example/stream");
  const proxy = await createStrictEgressProxy({
    policy,
    maxResponseBytes: 64,
    dial: () => net.connect(backendAddress.port, "127.0.0.1"),
  });

  try {
    const outcome = await proxyRequestOutcome({
      proxyPort: proxy.port,
      absoluteUrl: "http://public.example/stream",
    });

    assert.equal(outcome.complete, false);
    assert.ok(outcome.bytes <= 64);
    assert.ok(proxy.violations.includes("proxy_response_resource_limit"));
  } finally {
    await proxy.close();
    await closeServer(backend);
  }
});


test("seals the proxy before capture acceptance and rejects all later connections", async () => {
  let dialed = false;
  const policy = new PublicNetworkPolicy({resolver: async () => ["93.184.216.34"]});
  await policy.authorizeUrl("http://public.example/article");
  const proxy = await createStrictEgressProxy({
    policy,
    dial: () => {
      dialed = true;
      throw new Error("must not dial after seal");
    },
  });

  try {
    proxy.seal();
    await assert.rejects(
      proxyRequest({
        proxyPort: proxy.port,
        absoluteUrl: "http://public.example/article",
      }),
    );
    assert.equal(dialed, false);
    assert.ok(proxy.blockedAfterSeal.includes("proxy_connection_after_seal"));
    assert.deepEqual(proxy.violations, []);
  } finally {
    await proxy.close();
  }
});


test("bounds blocked post-seal connections and escalates flooding", async () => {
  const policy = new PublicNetworkPolicy({resolver: async () => ["93.184.216.34"]});
  await policy.authorizeUrl("http://public.example/article");
  const proxy = await createStrictEgressProxy({policy});

  try {
    proxy.seal();
    for (let index = 0; index < 300; index += 1) {
      await assert.rejects(
        proxyRequest({
          proxyPort: proxy.port,
          absoluteUrl: "http://public.example/article",
        }),
      );
    }

    assert.equal(proxy.blockedAfterSeal.length, 256);
    assert.ok(proxy.violations.includes("proxy_post_seal_limit"));
  } finally {
    await proxy.close();
  }
});


test("bounds fatal proxy violation accounting under request flooding", async () => {
  const policy = new PublicNetworkPolicy({resolver: async () => ["93.184.216.34"]});
  await policy.authorizeUrl("http://public.example/telemetry");
  const proxy = await createStrictEgressProxy({policy});

  try {
    for (let index = 0; index < 300; index += 1) {
      const response = await proxyRequest({
        proxyPort: proxy.port,
        absoluteUrl: "http://public.example/telemetry",
        method: "POST",
      });
      assert.equal(response.statusCode, 403);
    }

    assert.equal(proxy.violations.length, 256);
    assert.equal(proxy.violations.at(-1), "proxy_violation_limit");
  } finally {
    await proxy.close();
  }
});
