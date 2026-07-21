import assert from "node:assert/strict";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import { PublicNetworkPolicy } from "../../scripts/lib/secondary-network-policy.mjs";
import {
  capturePageWithRawCdp,
  discoverRawCdpWebSocket,
} from "../../scripts/lib/raw-cdp-capture.mjs";


class FakeWebSocket {
  constructor(harness) {
    this.harness = harness;
    this.readyState = 0;
    this.listeners = new Map();
    queueMicrotask(() => {
      this.readyState = 1;
      this.emit("open", {});
    });
  }

  addEventListener(name, listener) {
    const listeners = this.listeners.get(name) || new Set();
    listeners.add(listener);
    this.listeners.set(name, listeners);
  }

  removeEventListener(name, listener) {
    this.listeners.get(name)?.delete(listener);
  }

  emit(name, event) {
    for (const listener of this.listeners.get(name) || []) {
      listener(event);
    }
  }

  emitMessage(payload) {
    this.emit("message", {data: JSON.stringify(payload)});
  }

  send(raw) {
    this.harness.receive(this, JSON.parse(raw));
  }

  close() {
    if (this.readyState === 3) return;
    this.readyState = 3;
    this.harness.closed = true;
    this.emit("close", {code: 1000, reason: "", wasClean: true});
  }
}


class CdpHarness {
  constructor({
    unsafeUrl = null,
    download = false,
    missingContextId = false,
    missingTargetId = false,
    missingSessionId = false,
    uninterceptedUrl = null,
    duplicateUninterceptedInitialUrl = false,
    passiveResourceType = null,
    childTargetType = null,
    networkTransportMethod = null,
    extraNetworkEvents = 0,
    navigationErrorTexts = [],
    navigateIsDownloadResult = false,
    omitInitialFetch = false,
    continueProtocolError = false,
    unsafeMethod = null,
    unsafeRequestBody = false,
    failRequestProtocolError = false,
    responseDataEvents = [],
    lateUninterceptedAfterStop = false,
    documentResponseStatus = null,
    missingNavigationFrameId = false,
    unsafeSubresourceUrl = null,
    lateContinueProtocolError = false,
    observationFailure = null,
    frameNavigationUrl = null,
    frameNavigationFrameId = "frame-1",
    frameNavigationEvent = "Page.frameRequestedNavigation",
    pageSnapshots = null,
    disconnectAfterStop = false,
    invalidFetchUrlValue = undefined,
    invalidNetworkUrlValue = undefined,
    invalidFetchResourceType = "Document",
    invalidNetworkResourceType = "Fetch",
    invalidFetchCompanionNetworkResourceType = null,
    invalidNetworkLoadingFailedReason = null,
    invalidNetworkLoadingFailedFirst = false,
    invalidNetworkLoadingFailedResourceType = null,
    invalidNetworkRequestId = "network-invalid-url",
    invalidNetworkEventRepeats = 1,
    invalidNetworkLoadingFailedRepeats = 1,
  } = {}) {
    this.unsafeUrl = unsafeUrl;
    this.download = download;
    this.missingContextId = missingContextId;
    this.missingTargetId = missingTargetId;
    this.missingSessionId = missingSessionId;
    this.uninterceptedUrl = uninterceptedUrl;
    this.duplicateUninterceptedInitialUrl = duplicateUninterceptedInitialUrl;
    this.passiveResourceType = passiveResourceType;
    this.childTargetType = childTargetType;
    this.networkTransportMethod = networkTransportMethod;
    this.extraNetworkEvents = extraNetworkEvents;
    this.navigationErrorTexts = [...navigationErrorTexts];
    this.navigateIsDownloadResult = navigateIsDownloadResult;
    this.navigateCount = 0;
    this.omitInitialFetch = omitInitialFetch;
    this.continueProtocolError = continueProtocolError;
    this.unsafeMethod = unsafeMethod;
    this.unsafeRequestBody = unsafeRequestBody;
    this.failRequestProtocolError = failRequestProtocolError;
    this.responseDataEvents = [...responseDataEvents];
    this.lateUninterceptedAfterStop = lateUninterceptedAfterStop;
    this.documentResponseStatus = documentResponseStatus;
    this.missingNavigationFrameId = missingNavigationFrameId;
    this.unsafeSubresourceUrl = unsafeSubresourceUrl;
    this.lateContinueProtocolError = lateContinueProtocolError;
    this.observationFailure = observationFailure;
    this.frameNavigationUrl = frameNavigationUrl;
    this.frameNavigationFrameId = frameNavigationFrameId;
    this.frameNavigationEvent = frameNavigationEvent;
    this.pageSnapshots = pageSnapshots;
    this.disconnectAfterStop = disconnectAfterStop;
    this.invalidFetchUrlValue = invalidFetchUrlValue;
    this.invalidNetworkUrlValue = invalidNetworkUrlValue;
    this.invalidFetchResourceType = invalidFetchResourceType;
    this.invalidNetworkResourceType = invalidNetworkResourceType;
    this.invalidFetchCompanionNetworkResourceType =
      invalidFetchCompanionNetworkResourceType;
    this.invalidNetworkLoadingFailedReason = invalidNetworkLoadingFailedReason;
    this.invalidNetworkLoadingFailedFirst = invalidNetworkLoadingFailedFirst;
    this.invalidNetworkLoadingFailedResourceType =
      invalidNetworkLoadingFailedResourceType;
    this.invalidNetworkRequestId = invalidNetworkRequestId;
    this.invalidNetworkEventRepeats = invalidNetworkEventRepeats;
    this.invalidNetworkLoadingFailedRepeats = invalidNetworkLoadingFailedRepeats;
    this.captureEvaluateCount = 0;
    this.commands = [];
    this.closed = false;
    this.pendingNavigate = null;
    this.initialContinued = false;
  }

  respond(socket, message, result = {}) {
    queueMicrotask(() => socket.emitMessage({
      id: message.id,
      sessionId: message.sessionId,
      result,
    }));
  }

  respondError(socket, message, errorMessage) {
    queueMicrotask(() => socket.emitMessage({
      id: message.id,
      sessionId: message.sessionId,
      error: {code: -32000, message: errorMessage},
    }));
  }

  emitPaused(
    socket,
    url,
    requestId,
    redirectedRequestId = undefined,
    method = "GET",
    resourceType = "Document",
    hasPostData = false,
  ) {
    queueMicrotask(() => socket.emitMessage({
      method: "Network.requestWillBeSent",
      sessionId: "session-1",
      params: {
        requestId: `network-${requestId}`,
        loaderId: "loader-1",
        documentURL: url,
        request: {
          url,
          method,
          headers: {},
          ...(hasPostData ? {hasPostData: true, postData: "side-effect"} : {}),
        },
        timestamp: 1,
        wallTime: 1,
        initiator: {type: "other"},
        type: resourceType,
        frameId: "frame-1",
      },
    }));
    queueMicrotask(() => socket.emitMessage({
      method: "Fetch.requestPaused",
      sessionId: "session-1",
      params: {
        requestId,
        request: {
          url,
          method,
          headers: {},
          initialPriority: "VeryHigh",
          referrerPolicy: "strict-origin-when-cross-origin",
          ...(hasPostData ? {hasPostData: true, postData: "side-effect"} : {}),
        },
        frameId: "frame-1",
        resourceType,
        networkId: `network-${requestId}`,
        ...(redirectedRequestId ? {redirectedRequestId} : {}),
      },
    }));
  }

  finishNavigation(socket) {
    const pending = this.pendingNavigate;
    this.pendingNavigate = null;
    if (this.documentResponseStatus !== null) {
      queueMicrotask(() => socket.emitMessage({
        method: "Network.responseReceived",
        sessionId: "session-1",
        params: {
          requestId: "network-fetch-initial",
          loaderId: "loader-1",
          timestamp: 2,
          type: "Document",
          frameId: "frame-1",
          response: {
            url: pending.params.url,
            status: this.documentResponseStatus,
            statusText: this.documentResponseStatus === 403 ? "Forbidden" : "Error",
          },
        },
      }));
    }
    if (this.download) {
      queueMicrotask(() => socket.emitMessage({
        method: "Browser.downloadWillBegin",
        params: {
          frameId: "frame-1",
          guid: "download-guid",
          url: "https://public.example/file.pdf",
          suggestedFilename: "file.pdf",
        },
      }));
    }
    for (const [index, event] of this.responseDataEvents.entries()) {
      queueMicrotask(() => socket.emitMessage({
        method: "Network.dataReceived",
        sessionId: "session-1",
        params: {
          requestId: event.requestId || `response-${index}`,
          timestamp: 2,
          dataLength: event.dataLength,
          encodedDataLength: event.encodedDataLength,
        },
      }));
    }
    this.respond(socket, pending, {
      ...(!this.missingNavigationFrameId ? {frameId: "frame-1"} : {}),
      loaderId: "loader-1",
      ...(this.navigateIsDownloadResult ? {isDownload: true} : {}),
    });
  }

  receive(socket, message) {
    this.commands.push(message);
    switch (message.method) {
      case "Target.createBrowserContext":
        this.respond(
          socket,
          message,
          this.missingContextId ? {} : {browserContextId: "context-1"},
        );
        return;
      case "Target.createTarget":
        this.respond(socket, message, this.missingTargetId ? {} : {targetId: "target-1"});
        return;
      case "Target.attachToTarget":
        this.respond(socket, message, this.missingSessionId ? {} : {sessionId: "session-1"});
        return;
      case "Page.navigate":
        this.navigateCount += 1;
        if (this.navigationErrorTexts.length) {
          this.respond(socket, message, {
            frameId: "frame-1",
            errorText: this.navigationErrorTexts.shift(),
          });
          return;
        }
        this.pendingNavigate = message;
        if (this.omitInitialFetch) {
          queueMicrotask(() => socket.emitMessage({
            method: "Network.requestWillBeSent",
            sessionId: "session-1",
            params: {
              requestId: "network-initial-without-fetch",
              loaderId: "loader-1",
              documentURL: message.params.url,
              request: {url: message.params.url, method: "GET", headers: {}},
              timestamp: 1,
              wallTime: 1,
              initiator: {type: "other"},
              type: "Document",
              frameId: "frame-1",
            },
          }));
          this.finishNavigation(socket);
          return;
        }
        this.emitPaused(socket, message.params.url, "fetch-initial");
        return;
      case "Fetch.continueRequest":
        if (
          this.lateContinueProtocolError &&
          message.params.requestId === "fetch-late-after-stop"
        ) {
          this.respondError(socket, message, "late continue failed");
          return;
        }
        if (this.continueProtocolError) {
          this.respondError(socket, message, "continue failed");
          return;
        }
        this.respond(socket, message);
        if (!this.initialContinued) {
          this.initialContinued = true;
          if (this.unsafeUrl) {
            this.emitPaused(socket, this.unsafeUrl, "fetch-redirect", "fetch-initial");
          } else if (this.unsafeMethod) {
            this.emitPaused(
              socket,
              "https://public.example/telemetry",
              "fetch-unsafe-method",
              undefined,
              this.unsafeMethod,
              "Fetch",
            );
          } else if (this.unsafeRequestBody) {
            this.emitPaused(
              socket,
              "https://public.example/options-with-body",
              "fetch-request-body",
              undefined,
              "OPTIONS",
              "Fetch",
              true,
            );
          } else if (this.unsafeSubresourceUrl) {
            this.emitPaused(
              socket,
              this.unsafeSubresourceUrl,
              "fetch-unsafe-subresource",
              undefined,
              "GET",
              "Fetch",
            );
          } else {
            for (let index = 0; index < this.extraNetworkEvents; index += 1) {
              queueMicrotask(() => socket.emitMessage({
                method: "Network.requestWillBeSent",
                sessionId: "session-1",
                params: {
                  requestId: `network-flood-${index}`,
                  request: {
                    url: `https://public.example/flood?request=${index}`,
                    method: "GET",
                    headers: {},
                  },
                },
              }));
            }
            if (this.networkTransportMethod) {
              queueMicrotask(() => socket.emitMessage({
                method: this.networkTransportMethod,
                sessionId: "session-1",
                params: {
                  transportId: "transport-1",
                  identifier: "socket-1",
                  url: "https://public.example/transport",
                  timestamp: 2,
                },
              }));
            }
            if (this.childTargetType) {
              queueMicrotask(() => socket.emitMessage({
                method: "Target.attachedToTarget",
                sessionId: "session-1",
                params: {
                  sessionId: "child-session-1",
                  targetInfo: {
                    targetId: "child-target-1",
                    type: this.childTargetType,
                    title: "",
                    url: "",
                    attached: true,
                    browserContextId: "context-1",
                  },
                  waitingForDebugger: true,
                },
              }));
            }
            if (this.passiveResourceType) {
              this.emitPaused(
                socket,
                "https://public.example/passive-resource",
                "fetch-passive-resource",
                undefined,
                "GET",
                this.passiveResourceType,
              );
              return;
            }
            if (this.invalidFetchUrlValue !== undefined) {
              queueMicrotask(() => socket.emitMessage({
                method: "Fetch.requestPaused",
                sessionId: "session-1",
                params: {
                  requestId: "fetch-invalid-url",
                  request: {
                    url: this.invalidFetchUrlValue,
                    method: "GET",
                    headers: {},
                  },
                  frameId: "frame-1",
                  resourceType: this.invalidFetchResourceType,
                  networkId: "network-invalid-url",
                },
              }));
              if (this.invalidFetchCompanionNetworkResourceType !== null) {
                queueMicrotask(() => socket.emitMessage({
                  method: "Network.requestWillBeSent",
                  sessionId: "session-1",
                  params: {
                    requestId: "network-invalid-url",
                    request: {
                      url: this.invalidFetchUrlValue,
                      method: "GET",
                      headers: {},
                    },
                    type: this.invalidFetchCompanionNetworkResourceType,
                    frameId: "frame-1",
                  },
                }));
              }
              return;
            }
            if (this.invalidNetworkUrlValue !== undefined) {
              const emitLoadingFailed = () => socket.emitMessage({
                method: "Network.loadingFailed",
                sessionId: "session-1",
                params: {
                  requestId: this.invalidNetworkRequestId,
                  timestamp: 2,
                  type:
                    this.invalidNetworkLoadingFailedResourceType ||
                    this.invalidNetworkResourceType,
                  errorText: "net::ERR_BLOCKED_BY_CLIENT",
                  canceled: true,
                  blockedReason: this.invalidNetworkLoadingFailedReason,
                },
              });
              if (
                this.invalidNetworkLoadingFailedReason !== null &&
                this.invalidNetworkLoadingFailedFirst
              ) {
                for (
                  let index = 0;
                  index < this.invalidNetworkLoadingFailedRepeats;
                  index += 1
                ) {
                  queueMicrotask(emitLoadingFailed);
                }
              }
              for (let index = 0; index < this.invalidNetworkEventRepeats; index += 1) {
                queueMicrotask(() => socket.emitMessage({
                  method: "Network.requestWillBeSent",
                  sessionId: "session-1",
                  params: {
                    requestId: this.invalidNetworkRequestId,
                    request: {
                      url: this.invalidNetworkUrlValue,
                      method: "GET",
                      headers: {},
                    },
                    type: this.invalidNetworkResourceType,
                    frameId: "frame-1",
                  },
                }));
              }
              if (
                this.invalidNetworkLoadingFailedReason !== null &&
                !this.invalidNetworkLoadingFailedFirst
              ) {
                for (
                  let index = 0;
                  index < this.invalidNetworkLoadingFailedRepeats;
                  index += 1
                ) {
                  queueMicrotask(emitLoadingFailed);
                }
              }
              this.finishNavigation(socket);
              return;
            }
            if (this.duplicateUninterceptedInitialUrl) {
              queueMicrotask(() => socket.emitMessage({
                method: "Network.requestWillBeSent",
                sessionId: "session-1",
                params: {
                  requestId: "network-duplicate-without-fetch",
                  loaderId: "loader-duplicate",
                  documentURL: "https://public.example/start?scene=334",
                  request: {
                    url: "https://public.example/start?scene=334",
                    method: "GET",
                    headers: {},
                  },
                  timestamp: 2,
                  wallTime: 2,
                  initiator: {type: "script"},
                  type: "Fetch",
                  frameId: "frame-1",
                },
              }));
            }
            if (this.uninterceptedUrl) {
              queueMicrotask(() => socket.emitMessage({
                method: "Network.requestWillBeSent",
                sessionId: "session-1",
                params: {
                  requestId: "network-unintercepted",
                  loaderId: "loader-1",
                  documentURL: this.uninterceptedUrl,
                  request: {url: this.uninterceptedUrl, method: "GET", headers: {}},
                  timestamp: 2,
                  wallTime: 2,
                  initiator: {type: "script"},
                  type: "Fetch",
                  frameId: "frame-1",
                },
              }));
            }
            if (this.frameNavigationUrl) {
              queueMicrotask(() => socket.emitMessage({
                method: this.frameNavigationEvent,
                sessionId: "session-1",
                params: this.frameNavigationEvent === "Page.frameNavigated"
                  ? {
                      frame: {
                        id: this.frameNavigationFrameId,
                        url: this.frameNavigationUrl,
                      },
                    }
                  : {
                      frameId: this.frameNavigationFrameId,
                      reason: "scriptInitiated",
                      url: this.frameNavigationUrl,
                      disposition: "currentTab",
                    },
              }));
            }
            this.finishNavigation(socket);
          }
        } else if (this.pendingNavigate) {
          this.finishNavigation(socket);
        }
        return;
      case "Fetch.failRequest":
        if (
          this.failRequestProtocolError &&
          ["fetch-unsafe-method", "fetch-request-body", "fetch-invalid-url"].includes(
            message.params.requestId,
          )
        ) {
          this.respondError(socket, message, "failRequest failed");
        } else {
          this.respond(socket, message);
        }
        if (this.pendingNavigate) {
          this.finishNavigation(socket);
        }
        return;
      case "Runtime.evaluate":
        if (message.params.expression.includes("__paperReaderStrictNetworkGuard")) {
          this.respond(socket, message, {
            result: {type: "boolean", value: true},
          });
          return;
        }
        this.captureEvaluateCount += 1;
        const defaultSnapshot = {
          title: "Public article",
          description: "Description",
          publisher: "Publisher",
          publishedAt: "2026-07-16",
          finalUrl: "https://public.example/final",
          readyState: "complete",
          text: "可见正文".repeat(80),
        };
        const snapshots = this.pageSnapshots || [defaultSnapshot];
        const snapshot = snapshots[Math.min(
          this.captureEvaluateCount - 1,
          snapshots.length - 1,
        )];
        this.respond(socket, message, {
          result: {
            type: "string",
            value: JSON.stringify(snapshot),
          },
        });
        if (this.observationFailure === "disconnect") {
          queueMicrotask(() => socket.close());
        } else if (this.observationFailure === "malformed_message") {
          queueMicrotask(() => socket.emit("message", {data: "{"}));
        } else if (this.observationFailure === "oversized_message") {
          queueMicrotask(() => socket.emit("message", {data: "x".repeat(2 * 1024 * 1024 + 1)}));
        }
        return;
      case "Page.stopLoading":
        this.respond(socket, message);
        if (this.disconnectAfterStop) {
          queueMicrotask(() => socket.close());
        }
        if (this.lateUninterceptedAfterStop) {
          setTimeout(() => socket.emitMessage({
            method: "Network.requestWillBeSent",
            sessionId: "session-1",
            params: {
              requestId: "network-late-without-fetch",
              request: {
                url: "https://public.example/late",
                method: "GET",
                headers: {},
              },
            },
          }), 20);
        }
        if (this.lateContinueProtocolError) {
          setTimeout(() => this.emitPaused(
            socket,
            "https://public.example/late-after-stop",
            "fetch-late-after-stop",
            undefined,
            "GET",
            "Fetch",
          ), 20);
        }
        return;
      default:
        this.respond(socket, message);
    }
  }
}


async function runCapture(harness, captureOptions = {}) {
  const policy = new PublicNetworkPolicy({resolver: async () => ["93.184.216.34"]});
  const proxy = {
    host: "127.0.0.1",
    port: 45678,
    violations: [],
    blockedConnects: [],
    blockedAfterSeal: [],
    sealed: false,
    seal() {
      this.sealed = true;
    },
  };
  const capture = await capturePageWithRawCdp({
    wsEndpoint: "ws://127.0.0.1:9222/devtools/browser/test",
    sourceUrl: "https://public.example/start?scene=334",
    policy,
    proxy,
    expression: "capture-expression",
    deadline: Date.now() + 2000,
    pollMs: 1,
    requestRetryMs: 1,
    websocketFactory: () => new FakeWebSocket(harness),
    ...captureOptions,
  });
  return {capture, policy, proxy};
}


const INVALID_URL_SECRET = "paper-reader-invalid-url-secret.example/private?token=do-not-log";
const INVALID_NETWORK_URL_CASES = [
  {
    name: "non-string",
    value: {secret: INVALID_URL_SECRET},
    valueType: "object",
    lengthBucket: "not_applicable",
    parseability: "not_attempted",
    schemeClass: "none",
  },
  {
    name: "empty",
    value: "",
    valueType: "string",
    lengthBucket: "empty",
    parseability: "not_attempted",
    schemeClass: "none",
  },
  {
    name: "over-limit",
    value: `https://${INVALID_URL_SECRET}/${"x".repeat(4097)}`,
    valueType: "string",
    lengthBucket: "over_limit",
    parseability: "not_attempted",
    schemeClass: "https",
  },
  {
    name: "unparsable",
    value: `http://[${INVALID_URL_SECRET}`,
    valueType: "string",
    lengthBucket: "bounded",
    parseability: "invalid",
    schemeClass: "http",
  },
];


function invalidUrlDiagnostic({
  checkpoint,
  resourceClass,
  valueType,
  lengthBucket,
  parseability,
  schemeClass,
}) {
  return (
    "invalid_network_request_url:" +
    `checkpoint=${checkpoint};resource=${resourceClass};value=${valueType};` +
    `length=${lengthBucket};parse=${parseability};scheme=${schemeClass}`
  );
}


test("discovers a loopback browser WebSocket from a stable DevToolsActivePort file", async () => {
  const directory = fs.mkdtempSync(
    path.join(fs.realpathSync(os.tmpdir()), "paper-reader-cdp-test-"),
  );
  const activePort = path.join(directory, "DevToolsActivePort");
  fs.writeFileSync(
    activePort,
    "9222\n/devtools/browser/80a6e0a6-3dad-42a5-adf2-a6bb2e5f7dfb\n",
    {encoding: "utf8", mode: 0o600},
  );

  const endpoint = await discoverRawCdpWebSocket({
    explicitEndpoint: null,
    activePortPaths: [activePort],
    httpBaseUrls: [],
  });

  assert.equal(
    endpoint,
    "ws://127.0.0.1:9222/devtools/browser/80a6e0a6-3dad-42a5-adf2-a6bb2e5f7dfb",
  );
});


test("rejects a non-loopback explicit raw CDP endpoint before discovery fallback", async () => {
  await assert.rejects(
    discoverRawCdpWebSocket({
      explicitEndpoint: "ws://attacker.example/devtools/browser/token",
      activePortPaths: [],
      httpBaseUrls: [],
    }),
    /unsafe_raw_cdp_endpoint/,
  );
});


test("normalizes localhost CDP endpoints to a loopback literal before connecting", async () => {
  const endpoint = await discoverRawCdpWebSocket({
    explicitEndpoint: "ws://localhost:9222/devtools/browser/token",
    activePortPaths: [],
    httpBaseUrls: [],
  });

  assert.equal(endpoint, "ws://127.0.0.1:9222/devtools/browser/token");
});


test("falls back to a loopback json version endpoint", async () => {
  const server = http.createServer((_request, response) => {
    const body = JSON.stringify({
      Browser: "Chrome/145.0.0.0",
      "Protocol-Version": "1.3",
      webSocketDebuggerUrl: "ws://127.0.0.1:9333/devtools/browser/fallback-token",
    });
    response.writeHead(200, {
      "content-type": "application/json",
      "content-length": Buffer.byteLength(body),
    });
    response.end(body);
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  try {
    const endpoint = await discoverRawCdpWebSocket({
      explicitEndpoint: null,
      activePortPaths: [],
      httpBaseUrls: [`http://127.0.0.1:${address.port}`],
    });
    assert.equal(
      endpoint,
      "ws://127.0.0.1:9333/devtools/browser/fallback-token",
    );
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});


test("bounds all json version fallbacks by one caller deadline", async () => {
  const server = http.createServer(() => {});
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  const started = Date.now();
  try {
    await assert.rejects(
      discoverRawCdpWebSocket({
        explicitEndpoint: null,
        activePortPaths: [],
        httpBaseUrls: [`http://127.0.0.1:${address.port}`],
        deadline: Date.now() + 80,
      }),
      /raw_cdp_endpoint_unavailable/,
    );
    assert.ok(Date.now() - started < 500);
  } finally {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  }
});


test("installs all guards before navigating and captures through the isolated context", async () => {
  const harness = new CdpHarness();
  const {capture, policy} = await runCapture(harness);

  assert.equal(capture.status, "captured");
  assert.equal(capture.data.title, "Public article");
  assert.equal(policy.isUrlAuthorized("https://public.example/start?scene=334"), true);
  const methods = harness.commands.map((command) => command.method);
  const navigateIndex = methods.indexOf("Page.navigate");
  for (const required of [
    "Browser.setDownloadBehavior",
    "Fetch.enable",
    "Network.enable",
    "Network.setCacheDisabled",
    "Network.setBypassServiceWorker",
    "Network.setBlockedURLs",
    "Page.addScriptToEvaluateOnNewDocument",
    "Target.setAutoAttach",
  ]) {
    assert.ok(methods.indexOf(required) >= 0, `${required} must be sent`);
    assert.ok(methods.indexOf(required) < navigateIndex, `${required} must precede navigation`);
  }
  const context = harness.commands.find((command) => command.method === "Target.createBrowserContext");
  assert.deepEqual(context.params, {
    disposeOnDetach: true,
    proxyServer: "http://127.0.0.1:45678",
    proxyBypassList: "<-loopback>",
  });
  const target = harness.commands.find((command) => command.method === "Target.createTarget");
  assert.equal(target.params.url, "about:blank");
  assert.equal(target.params.browserContextId, "context-1");
  assert.equal(methods.at(-2), "Target.closeTarget");
  assert.equal(methods.at(-1), "Target.disposeBrowserContext");
  assert.equal(harness.closed, true);
  const networkEnable = harness.commands.find(
    (command) => command.method === "Network.enable",
  );
  assert.equal(networkEnable.params.reportDirectSocketTraffic, true);
  const hardening = harness.commands.find(
    (command) => command.method === "Page.addScriptToEvaluateOnNewDocument",
  );
  for (const api of [
    "RTCPeerConnection",
    "WebTransport",
    "WebSocket",
    "EventSource",
    "Worker",
    "SharedWorker",
  ]) {
    assert.match(hardening.params.source, new RegExp(api));
  }
  const blocked = harness.commands.find(
    (command) => command.method === "Network.setBlockedURLs",
  );
  for (const scheme of ["data:*", "blob:*", "mailto:*", "tel:*", "intent:*"]) {
    assert.ok(blocked.params.urls.includes(scheme), `${scheme} must be blocked`);
  }
});


test("fails closed when the CDP observation channel is lost after readable text", async () => {
  for (const [observationFailure, warning] of [
    ["disconnect", "raw_cdp_connection_lost"],
    ["malformed_message", "invalid_cdp_message"],
    ["oversized_message", "cdp_message_resource_limit"],
  ]) {
    const harness = new CdpHarness({observationFailure});
    const {capture} = await runCapture(harness);

    assert.equal(capture.status, "unavailable", observationFailure);
    assert.equal(capture.data.text, "", observationFailure);
    assert.ok(capture.warnings.includes(warning), observationFailure);
  }
});


test("fails closed when CDP disconnects during final capture quiescence", async () => {
  const harness = new CdpHarness({disconnectAfterStop: true});
  const {capture} = await runCapture(harness);

  assert.equal(capture.status, "unavailable");
  assert.equal(capture.data.text, "");
  assert.ok(capture.warnings.includes("raw_cdp_connection_lost"));
});


test("waits past a short loading placeholder for usable article content", async () => {
  const harness = new CdpHarness({
    pageSnapshots: [
      {
        title: "",
        description: "",
        publisher: "",
        publishedAt: "",
        finalUrl: "https://public.example/final",
        readyState: "complete",
        text: "Loading article...",
      },
      {
        title: "Loaded article",
        description: "Description",
        publisher: "Publisher",
        publishedAt: "2026-07-16",
        finalUrl: "https://public.example/final",
        readyState: "complete",
        text: "延迟加载后的正文".repeat(80),
      },
    ],
  });
  const {capture} = await runCapture(harness);

  assert.equal(capture.status, "captured");
  assert.equal(capture.data.title, "Loaded article");
  assert.match(capture.data.text, /延迟加载后的正文/);
  assert.ok(harness.captureEvaluateCount >= 2);
});


test("fails closed on top-level and iframe navigation to external schemes", async () => {
  for (const [frameNavigationUrl, frameNavigationFrameId, frameNavigationEvent] of [
    ["mailto:reader@example.org", "frame-1", "Page.frameRequestedNavigation"],
    ["custom-handler:launch", "iframe-1", "Page.frameNavigated"],
  ]) {
    const harness = new CdpHarness({
      frameNavigationUrl,
      frameNavigationFrameId,
      frameNavigationEvent,
    });
    const {capture} = await runCapture(harness);

    assert.equal(capture.status, "unavailable", frameNavigationUrl);
    assert.equal(capture.data.text, "", frameNavigationUrl);
    assert.ok(capture.warnings.includes("unsafe_navigation_blocked"));
    assert.ok(capture.warnings.includes(
      `unsafe_request_scheme:${new URL(frameNavigationUrl).protocol.slice(0, -1)}`,
    ));
    assert.ok(harness.commands.some(
      (command) => command.method === "Page.stopLoading",
    ));
  }
});


test("keeps a blocked browser-background CONNECT outside the owned target non-fatal", async () => {
  const harness = new CdpHarness();
  const proxy = {
    host: "127.0.0.1",
    port: 45678,
    violations: [],
    blockedConnects: [
      {protocol: "https:", hostname: "www.google.com", port: 443},
    ],
    blockedAfterSeal: [],
    seal() {},
  };
  const {capture} = await runCapture(harness, {proxy});

  assert.equal(capture.status, "captured");
  assert.ok(capture.warnings.includes("browser_background_connect_blocked"));
});


test("treats a blocked CONNECT observed in the owned target as fatal", async () => {
  const harness = new CdpHarness({
    uninterceptedUrl: "https://unexpected.example/resource",
  });
  const proxy = {
    host: "127.0.0.1",
    port: 45678,
    violations: [],
    blockedConnects: [
      {protocol: "https:", hostname: "unexpected.example", port: 443},
    ],
    blockedAfterSeal: [],
    seal() {},
  };
  const {capture} = await runCapture(harness, {proxy});

  assert.equal(capture.status, "unavailable");
  assert.ok(capture.warnings.includes("proxy_unauthorized_connect"));
});


test("records a pre-Fetch proxy race as recovered only after exact authorization", async () => {
  const harness = new CdpHarness();
  const proxy = {
    host: "127.0.0.1",
    port: 45678,
    violations: [],
    blockedConnects: [
      {protocol: "https:", hostname: "public.example", port: 443},
    ],
    blockedAfterSeal: [],
    seal() {},
  };
  const {capture} = await runCapture(harness, {proxy});

  assert.equal(capture.status, "captured");
  assert.ok(capture.warnings.includes("proxy_connect_race_recovered"));
});


test("keeps bounded proxy activity blocked after seal as an audit warning", async () => {
  const harness = new CdpHarness();
  const proxy = {
    host: "127.0.0.1",
    port: 45678,
    violations: [],
    blockedConnects: [],
    blockedAfterSeal: [],
    seal() {
      if (this.blockedAfterSeal.length === 0) {
        this.blockedAfterSeal.push("proxy_connection_after_seal");
      }
    },
  };
  const {capture} = await runCapture(harness, {proxy});

  assert.equal(capture.status, "captured");
  assert.ok(capture.warnings.includes("proxy_activity_after_seal_blocked"));
  assert.equal(capture.warnings.includes("proxy_policy_violation"), false);
});


test("hardens an auto-attached worker before it is allowed to execute", async () => {
  const harness = new CdpHarness({childTargetType: "worker"});
  const {capture} = await runCapture(harness);

  assert.equal(capture.status, "captured");
  const workerCommands = harness.commands.filter(
    (command) => command.sessionId === "child-session-1",
  );
  const evaluateIndex = workerCommands.findIndex(
    (command) =>
      command.method === "Runtime.evaluate" &&
      command.params.expression.includes("RTCPeerConnection"),
  );
  const resumeIndex = workerCommands.findIndex(
    (command) => command.method === "Runtime.runIfWaitingForDebugger",
  );
  assert.ok(evaluateIndex >= 0);
  assert.ok(resumeIndex > evaluateIndex);
});


test("fails closed when the guarded target-session total exceeds its cap", async () => {
  const harness = new CdpHarness({childTargetType: "worker"});
  const {capture} = await runCapture(harness, {maxGuardedSessions: 1});

  assert.equal(capture.status, "unavailable");
  assert.ok(capture.warnings.includes("target_resource_limit"));
  assert.equal(harness.closed, true);
  assert.equal(
    harness.commands.some(
      (command) =>
        command.sessionId === "child-session-1" &&
        command.method === "Runtime.runIfWaitingForDebugger",
    ),
    false,
  );
});


test("closes an auto-attached popup before accepting its content", async () => {
  const harness = new CdpHarness({childTargetType: "page"});
  const {capture} = await runCapture(harness);

  assert.equal(capture.status, "unavailable");
  assert.ok(capture.warnings.includes("popup_blocked"));
  const close = harness.commands.find(
    (command) =>
      command.method === "Target.closeTarget" &&
      command.params.targetId === "child-target-1",
  );
  assert.ok(close);
});


test("fails closed on transports outside Fetch HTTP interception", async () => {
  for (const method of [
    "Network.webTransportCreated",
    "Network.directTCPSocketCreated",
    "Network.directUDPSocketCreated",
  ]) {
    const harness = new CdpHarness({networkTransportMethod: method});
    const {capture} = await runCapture(harness);

    assert.equal(capture.status, "unavailable", method);
    assert.ok(capture.warnings.includes("unsupported_network_transport"), method);
  }
});


test("closes the owned CDP connection when request event accounting exceeds its cap", async () => {
  const harness = new CdpHarness({extraNetworkEvents: 2});

  await assert.rejects(
    runCapture(harness, {maxRequestEvents: 2}),
    /request_resource_limit/,
  );
  assert.equal(harness.closed, true);
});


test("retries an immediate transient navigation error on the same raw CDP connection", async () => {
  const harness = new CdpHarness({navigationErrorTexts: ["net::ERR_NETWORK_CHANGED"]});
  const {capture} = await runCapture(harness);

  assert.equal(capture.status, "captured");
  assert.equal(harness.navigateCount, 2);
});


test("does not retry a non-transient navigation policy failure", async () => {
  const harness = new CdpHarness({
    navigationErrorTexts: ["net::ERR_CERT_AUTHORITY_INVALID"],
  });
  const {capture} = await runCapture(harness);

  assert.equal(capture.status, "unavailable");
  assert.ok(capture.warnings.includes("navigation_failed"));
  assert.equal(harness.navigateCount, 1);
});


test("treats Page.navigate download result as blocked even without an event", async () => {
  const harness = new CdpHarness({navigateIsDownloadResult: true});
  const {capture} = await runCapture(harness);

  assert.equal(capture.status, "unavailable");
  assert.ok(capture.warnings.includes("download_blocked"));
});


test("marks a main-document HTTP 403 response unavailable", async () => {
  const harness = new CdpHarness({documentResponseStatus: 403});
  const {capture} = await runCapture(harness);

  assert.equal(capture.status, "unavailable");
  assert.ok(capture.warnings.includes("http_403_unauthorized"));
});


test("fails closed when Page.navigate omits its root frame identity", async () => {
  const harness = new CdpHarness({missingNavigationFrameId: true});

  await assert.rejects(runCapture(harness), /invalid_navigation_frame/);
  assert.equal(harness.closed, true);
});


test("classifies invalid Fetch and Network URL values without disclosing them", async () => {
  for (const checkpoint of ["fetch_request_paused", "network_request_will_be_sent"]) {
    for (const invalidCase of INVALID_NETWORK_URL_CASES) {
      const harness = new CdpHarness(
        checkpoint === "fetch_request_paused"
          ? {invalidFetchUrlValue: invalidCase.value}
          : {invalidNetworkUrlValue: invalidCase.value},
      );
      const {capture} = await runCapture(harness);
      const expectedDiagnostic = invalidUrlDiagnostic({
        checkpoint,
        resourceClass: checkpoint === "fetch_request_paused" ? "document" : "fetch",
        valueType: invalidCase.valueType,
        lengthBucket: invalidCase.lengthBucket,
        parseability: invalidCase.parseability,
        schemeClass: invalidCase.schemeClass,
      });

      assert.equal(capture.status, "unavailable", `${checkpoint}:${invalidCase.name}`);
      assert.equal(capture.data.text, "", `${checkpoint}:${invalidCase.name}`);
      assert.ok(
        capture.warnings.includes("invalid_network_request_url"),
        `${checkpoint}:${invalidCase.name}`,
      );
      assert.ok(
        capture.warnings.includes(expectedDiagnostic),
        `${checkpoint}:${invalidCase.name}:${JSON.stringify(capture.warnings)}`,
      );
      assert.equal(
        JSON.stringify(capture).includes(INVALID_URL_SECRET),
        false,
        `${checkpoint}:${invalidCase.name}`,
      );
      const invalidCancellation = harness.commands.find(
        (command) =>
          command.method === "Fetch.failRequest" &&
          command.params.requestId === "fetch-invalid-url",
      );
      if (checkpoint === "fetch_request_paused") {
        assert.equal(invalidCancellation.params.errorReason, "BlockedByClient");
      } else {
        assert.equal(invalidCancellation, undefined);
      }
    }
  }
});


test("blocks oversized inline passive data resources at Fetch without URL failures", async () => {
  const inlineDataUrl = `data:application/octet-stream;base64,${"A".repeat(5000)}`;
  for (const resourceType of ["Font", "Image", "Media", "Prefetch", "TextTrack"]) {
    const harness = new CdpHarness({
      invalidFetchUrlValue: inlineDataUrl,
      invalidFetchResourceType: resourceType,
    });
    const {capture} = await runCapture(harness);

    assert.equal(capture.status, "captured", resourceType);
    assert.equal(capture.warnings.includes("invalid_network_request_url"), false);
    const cancellation = harness.commands.find(
      (command) =>
        command.method === "Fetch.failRequest" &&
        command.params.requestId === "fetch-invalid-url",
    );
    assert.equal(cancellation.params.errorReason, "BlockedByClient", resourceType);
  }
});


test("accepts oversized passive data only after an inspector block proof", async () => {
  const inlineDataUrl = `data:font/woff2;base64,${"A".repeat(5000)}`;
  for (const loadingFailedFirst of [false, true]) {
    const harness = new CdpHarness({
      invalidNetworkUrlValue: inlineDataUrl,
      invalidNetworkResourceType: "Font",
      invalidNetworkLoadingFailedReason: "inspector",
      invalidNetworkLoadingFailedFirst: loadingFailedFirst,
    });
    const {capture} = await runCapture(harness);

    assert.equal(capture.status, "captured", String(loadingFailedFirst));
    assert.equal(capture.warnings.includes("invalid_network_request_url"), false);
  }
});


test("fails closed for oversized passive data observed only by Network", async () => {
  const harness = new CdpHarness({
    invalidNetworkUrlValue: `data:font/woff2;base64,${"A".repeat(5000)}`,
    invalidNetworkResourceType: "Font",
  });
  const {capture} = await runCapture(harness);

  assert.equal(capture.status, "unavailable");
  assert.ok(capture.warnings.includes("unproven_passive_data_block"));
});


test("does not accept a non-inspector or type-drifted passive data block proof", async () => {
  for (const options of [
    {invalidNetworkLoadingFailedReason: "other"},
    {
      invalidNetworkLoadingFailedReason: "inspector",
      invalidNetworkLoadingFailedResourceType: "Fetch",
    },
  ]) {
    const harness = new CdpHarness({
      invalidNetworkUrlValue: `data:font/woff2;base64,${"A".repeat(5000)}`,
      invalidNetworkResourceType: "Font",
      ...options,
    });
    const {capture} = await runCapture(harness);

    assert.equal(capture.status, "unavailable");
    assert.ok(capture.warnings.includes("unproven_passive_data_block"));
  }
});


test("accepts matching Fetch cancellation and Network observation for passive data", async () => {
  const harness = new CdpHarness({
    invalidFetchUrlValue: `data:font/woff2;base64,${"A".repeat(5000)}`,
    invalidFetchResourceType: "Font",
    invalidFetchCompanionNetworkResourceType: "Font",
  });
  const {capture} = await runCapture(harness);

  assert.equal(capture.status, "captured");
  assert.equal(capture.warnings.includes("invalid_network_request_url"), false);
});


test("fails closed for duplicate or malformed passive data Network identities", async () => {
  for (const options of [
    {invalidNetworkEventRepeats: 2},
    {invalidNetworkRequestId: ""},
    {invalidNetworkRequestId: "x".repeat(513)},
  ]) {
    const harness = new CdpHarness({
      invalidNetworkUrlValue: `data:font/woff2;base64,${"A".repeat(5000)}`,
      invalidNetworkResourceType: "Font",
      invalidNetworkLoadingFailedReason: "inspector",
      ...options,
    });
    const {capture} = await runCapture(harness);

    assert.equal(capture.status, "unavailable");
  }
});


test("bounds passive data block proof events", async () => {
  const harness = new CdpHarness({
    invalidNetworkUrlValue: `data:font/woff2;base64,${"A".repeat(5000)}`,
    invalidNetworkResourceType: "Font",
    invalidNetworkLoadingFailedReason: "inspector",
    invalidNetworkLoadingFailedRepeats: 3,
  });

  await assert.rejects(
    runCapture(harness, {maxRequestEvents: 3}),
    /request_resource_limit/,
  );
  assert.equal(harness.closed, true);
});


test("keeps oversized inline data font cancellation failure fatal", async () => {
  const harness = new CdpHarness({
    invalidFetchUrlValue: `data:font/woff2;base64,${"A".repeat(5000)}`,
    invalidFetchResourceType: "Font",
    failRequestProtocolError: true,
  });
  const {capture, proxy} = await runCapture(harness);

  assert.equal(capture.status, "unavailable");
  assert.equal(capture.warnings.includes("invalid_network_request_url"), false);
  assert.ok(capture.warnings.includes("cdp_fail_request_failed"));
  assert.equal(proxy.sealed, true);
  assert.equal(harness.closed, true);
});


test("does not exempt active or non-data oversized URLs from strict accounting", async () => {
  const cases = [
    {value: `data:text/javascript,${"x".repeat(5000)}`, resourceType: "Script"},
    {value: `data:text/html,${"x".repeat(5000)}`, resourceType: "Document"},
    {value: `blob:https://public.example/${"x".repeat(5000)}`, resourceType: "Font"},
    {value: `https://public.example/${"x".repeat(5000)}`, resourceType: "Font"},
  ];
  for (const {value, resourceType} of cases) {
    const harness = new CdpHarness({
      invalidFetchUrlValue: value,
      invalidFetchResourceType: resourceType,
    });
    const {capture} = await runCapture(harness);

    assert.equal(capture.status, "unavailable", resourceType);
    assert.ok(capture.warnings.includes("invalid_network_request_url"), resourceType);
    const cancellation = harness.commands.find(
      (command) =>
        command.method === "Fetch.failRequest" &&
        command.params.requestId === "fetch-invalid-url",
    );
    assert.equal(cancellation.params.errorReason, "BlockedByClient", resourceType);
  }
});


test("fails closed when Fetch and Network disagree that oversized data is passive", async () => {
  const harness = new CdpHarness({
    invalidFetchUrlValue: `data:font/woff2;base64,${"A".repeat(5000)}`,
    invalidFetchResourceType: "Font",
    invalidFetchCompanionNetworkResourceType: "Fetch",
  });
  const {capture} = await runCapture(harness);

  assert.equal(capture.status, "unavailable");
  assert.ok(capture.warnings.includes("invalid_network_request_url"));
  const cancellation = harness.commands.find(
    (command) =>
      command.method === "Fetch.failRequest" &&
      command.params.requestId === "fetch-invalid-url",
  );
  assert.equal(cancellation.params.errorReason, "BlockedByClient");
});


test("keeps invalid Fetch URL cancellation failure fatal without disclosing the value", async () => {
  const harness = new CdpHarness({
    invalidFetchUrlValue: `http://[${INVALID_URL_SECRET}`,
    failRequestProtocolError: true,
  });
  const {capture, proxy} = await runCapture(harness);

  assert.equal(capture.status, "unavailable");
  assert.ok(capture.warnings.includes("invalid_network_request_url"));
  assert.ok(capture.warnings.includes("cdp_fail_request_failed"));
  assert.equal(JSON.stringify(capture).includes(INVALID_URL_SECRET), false);
  assert.equal(proxy.sealed, true);
  assert.equal(harness.closed, true);
});


test("bounds invalid URL resource classification to a fixed other bucket", async () => {
  const harness = new CdpHarness({
    invalidFetchUrlValue: "",
    invalidFetchResourceType: "Document".repeat(1024),
  });
  const {capture} = await runCapture(harness);

  assert.ok(capture.warnings.includes(invalidUrlDiagnostic({
    checkpoint: "fetch_request_paused",
    resourceClass: "other",
    valueType: "string",
    lengthBucket: "empty",
    parseability: "not_attempted",
    schemeClass: "none",
  })));
  assert.equal(capture.warnings.some((warning) => warning.length > 256), false);
});


test("fails an unsafe redirect and returns unavailable without accepting page text", async () => {
  const harness = new CdpHarness({unsafeUrl: "http://127.0.0.1/private"});
  const {capture} = await runCapture(harness);

  assert.equal(capture.status, "unavailable");
  assert.equal(capture.data.text, "");
  assert.ok(capture.warnings.includes("unsafe_request_blocked"));
  assert.ok(capture.warnings.includes("unsafe_request_scheme:http"));
  const failed = harness.commands.find((command) => command.method === "Fetch.failRequest");
  assert.equal(failed.params.requestId, "fetch-redirect");
  assert.equal(failed.params.errorReason, "BlockedByClient");
  assert.equal(harness.commands.at(-1).method, "Target.disposeBrowserContext");
});


test("denies and cancels a download and returns unavailable", async () => {
  const harness = new CdpHarness({download: true});
  const {capture} = await runCapture(harness);

  assert.equal(capture.status, "unavailable");
  assert.ok(capture.warnings.includes("download_blocked"));
  const deny = harness.commands.find((command) => command.method === "Browser.setDownloadBehavior");
  assert.deepEqual(deny.params, {
    behavior: "deny",
    browserContextId: "context-1",
    eventsEnabled: true,
  });
  const cancel = harness.commands.find((command) => command.method === "Browser.cancelDownload");
  assert.equal(cancel.params.guid, "download-guid");
  assert.equal(cancel.params.browserContextId, "context-1");
});


test("blocks passive binary subresources without discarding readable article text", async () => {
  for (const resourceType of ["Image", "Media", "Font", "TextTrack", "Prefetch"]) {
    const harness = new CdpHarness({passiveResourceType: resourceType});
    const {capture, policy} = await runCapture(harness);

    assert.equal(capture.status, "captured", resourceType);
    const failed = harness.commands.find(
      (command) =>
        command.method === "Fetch.failRequest" &&
        command.params.requestId === "fetch-passive-resource",
    );
    assert.equal(failed.params.errorReason, "BlockedByClient", resourceType);
    assert.equal(
      policy.isUrlAuthorized("https://public.example/passive-resource"),
      false,
      resourceType,
    );
  }
});


test("fails closed when Network observes a request that Fetch never paused", async () => {
  const harness = new CdpHarness({
    uninterceptedUrl: "https://public.example/unintercepted",
  });
  const {capture} = await runCapture(harness);

  assert.equal(capture.status, "unavailable");
  assert.ok(capture.warnings.includes("unintercepted_network_request"));
  assert.ok(
    harness.commands.some((command) => command.method === "Page.stopLoading"),
  );
});


test("waits for late Network events after stopLoading before accepting a capture", async () => {
  const harness = new CdpHarness({lateUninterceptedAfterStop: true});
  const {capture, proxy} = await runCapture(harness, {eventQuiescenceMs: 40});

  assert.equal(proxy.sealed, true);
  assert.equal(capture.status, "unavailable");
  assert.ok(capture.warnings.includes("unintercepted_network_request"));
});


test("does not count source preflight as Fetch authorization for the first hop", async () => {
  const harness = new CdpHarness({omitInitialFetch: true});
  const {capture} = await runCapture(harness);

  assert.equal(capture.status, "unavailable");
  assert.ok(capture.warnings.includes("unintercepted_network_request"));
  assert.equal(
    harness.commands.some((command) => command.method === "Fetch.continueRequest"),
    false,
  );
});


test("detects an unpaused duplicate request even when the same URL was authorized once", async () => {
  const harness = new CdpHarness({duplicateUninterceptedInitialUrl: true});
  const {capture} = await runCapture(harness);

  assert.equal(capture.status, "unavailable");
  assert.ok(capture.warnings.includes("unintercepted_network_request"));
  assert.equal(
    harness.commands.filter((command) => command.method === "Fetch.continueRequest").length,
    1,
  );
});


test("reports a CDP continue failure without misclassifying the URL as unsafe", async () => {
  const harness = new CdpHarness({continueProtocolError: true});
  const {capture} = await runCapture(harness);

  assert.equal(capture.status, "unavailable");
  assert.ok(capture.warnings.includes("cdp_continue_failed"));
  assert.equal(capture.warnings.includes("unsafe_request_blocked"), false);
  assert.ok(
    harness.commands.some((command) => command.method === "Fetch.failRequest"),
  );
});


test("blocks an unsafe HTTP method to keep browser capture read-only", async () => {
  const harness = new CdpHarness({unsafeMethod: "POST"});
  const {capture} = await runCapture(harness);

  assert.equal(capture.status, "unavailable");
  assert.ok(capture.warnings.includes("unsafe_method_blocked"));
  const failed = harness.commands.find(
    (command) =>
      command.method === "Fetch.failRequest" &&
      command.params.requestId === "fetch-unsafe-method",
  );
  assert.equal(failed.params.errorReason, "BlockedByClient");
});


test("blocks a request body even when the HTTP method is otherwise safe", async () => {
  const harness = new CdpHarness({unsafeRequestBody: true});
  const {capture} = await runCapture(harness);

  assert.equal(capture.status, "unavailable");
  assert.ok(capture.warnings.includes("request_body_blocked"));
  const failed = harness.commands.find(
    (command) =>
      command.method === "Fetch.failRequest" &&
      command.params.requestId === "fetch-request-body",
  );
  assert.equal(failed.params.errorReason, "BlockedByClient");
});


test("fails closed when CDP cannot prove an unsafe request was cancelled", async () => {
  const harness = new CdpHarness({
    unsafeMethod: "POST",
    failRequestProtocolError: true,
  });
  const {capture} = await runCapture(harness);

  assert.equal(capture.status, "unavailable");
  assert.ok(capture.warnings.includes("cdp_fail_request_failed"));
});


test("fails closed when one decoded response exceeds its byte budget", async () => {
  const harness = new CdpHarness({
    responseDataEvents: [
      {requestId: "response-large", dataLength: 1025, encodedDataLength: 512},
    ],
  });
  const {capture, proxy} = await runCapture(harness, {
    maxSingleResponseBytes: 1024,
    maxAggregateResponseBytes: 4096,
  });

  assert.equal(proxy.sealed, true);
  assert.equal(capture.status, "unavailable");
  assert.ok(capture.warnings.includes("response_resource_limit"));
});


test("fails closed when aggregate response bytes exceed their budget", async () => {
  const harness = new CdpHarness({
    responseDataEvents: [
      {requestId: "response-a", dataLength: 700, encodedDataLength: 500},
      {requestId: "response-b", dataLength: 700, encodedDataLength: 500},
    ],
  });
  const {capture, proxy} = await runCapture(harness, {
    maxSingleResponseBytes: 1024,
    maxAggregateResponseBytes: 1200,
  });

  assert.equal(proxy.sealed, true);
  assert.equal(capture.status, "unavailable");
  assert.ok(capture.warnings.includes("response_resource_limit"));
});


test("keeps a blocked unsafe non-document subresource as an audit warning", async () => {
  const harness = new CdpHarness({
    unsafeSubresourceUrl: "http://127.0.0.1/private-subresource",
  });
  const {capture} = await runCapture(harness);

  assert.equal(capture.status, "captured");
  assert.ok(capture.warnings.includes("unsafe_subresource_blocked"));
  assert.ok(capture.warnings.includes("unsafe_request_scheme:http"));
  const failed = harness.commands.find(
    (command) =>
      command.method === "Fetch.failRequest" &&
      command.params.requestId === "fetch-unsafe-subresource",
  );
  assert.equal(failed.params.errorReason, "BlockedByClient");
});


test("keeps a continue failure after final stopLoading as an audit warning", async () => {
  const harness = new CdpHarness({lateContinueProtocolError: true});
  const {capture} = await runCapture(harness, {eventQuiescenceMs: 40});

  assert.equal(capture.status, "captured");
  assert.ok(capture.warnings.includes("late_request_cancelled"));
  assert.equal(capture.warnings.includes("cdp_continue_failed"), false);
});


test("fails before target creation when Chrome omits the isolated context id", async () => {
  const harness = new CdpHarness({missingContextId: true});

  await assert.rejects(runCapture(harness), /invalid_cdp_browser_context/);
  assert.equal(
    harness.commands.some((command) => command.method === "Target.createTarget"),
    false,
  );
});


test("seals the egress proxy when raw CDP setup throws", async () => {
  const harness = new CdpHarness({missingContextId: true});
  const policy = new PublicNetworkPolicy({resolver: async () => ["93.184.216.34"]});
  const proxy = {
    host: "127.0.0.1",
    port: 45678,
    violations: [],
    blockedConnects: [],
    blockedAfterSeal: [],
    sealed: false,
    seal() {
      this.sealed = true;
    },
  };

  await assert.rejects(
    capturePageWithRawCdp({
      wsEndpoint: "ws://127.0.0.1:9222/devtools/browser/test",
      sourceUrl: "https://public.example/start?scene=334",
      policy,
      proxy,
      expression: "capture-expression",
      deadline: Date.now() + 2000,
      pollMs: 1,
      requestRetryMs: 1,
      websocketFactory: () => new FakeWebSocket(harness),
    }),
    /invalid_cdp_browser_context/,
  );
  assert.equal(proxy.sealed, true);
});


test("disposes the context when Chrome omits the target id", async () => {
  const harness = new CdpHarness({missingTargetId: true});

  await assert.rejects(runCapture(harness), /invalid_cdp_target/);
  assert.equal(
    harness.commands.some((command) => command.method === "Target.attachToTarget"),
    false,
  );
  assert.equal(harness.commands.at(-1).method, "Target.disposeBrowserContext");
});


test("closes the target and context when Chrome omits the session id", async () => {
  const harness = new CdpHarness({missingSessionId: true});

  await assert.rejects(runCapture(harness), /invalid_cdp_session/);
  assert.deepEqual(
    harness.commands.slice(-2).map((command) => command.method),
    ["Target.closeTarget", "Target.disposeBrowserContext"],
  );
});
