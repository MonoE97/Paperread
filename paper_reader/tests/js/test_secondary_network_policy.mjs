import assert from "node:assert/strict";
import test from "node:test";

import {
  PublicNetworkPolicy,
  resolveSystemHostname,
} from "../../scripts/lib/secondary-network-policy.mjs";


test("authorizes an exact URL and pins one public address for the capture", async () => {
  const lookups = [];
  const policy = new PublicNetworkPolicy({
    resolver: async (hostname) => {
      lookups.push(hostname);
      return ["93.184.216.35", "93.184.216.34"];
    },
  });

  const first = await policy.authorizeUrl("https://Example.COM/article?scene=334&x=1");
  const second = await policy.authorizeUrl("https://example.com/other?x=2");

  assert.deepEqual(lookups, ["example.com"]);
  assert.equal(first.exactUrl, "https://example.com/article?scene=334&x=1");
  assert.equal(second.exactUrl, "https://example.com/other?x=2");
  assert.equal(first.address, "93.184.216.35");
  assert.equal(second.address, first.address);
  assert.equal(first.port, 443);
  assert.equal(first.hostname, "example.com");
  assert.equal(policy.isUrlAuthorized(first.exactUrl), true);
  assert.equal(policy.isAuthorityAuthorized("https:", "example.com", 443), true);
  assert.equal(policy.isAuthorityAuthorized("http:", "example.com", 443), false);
});


test("preflight validation pins DNS without claiming Fetch authorization", async () => {
  const policy = new PublicNetworkPolicy({resolver: async () => ["93.184.216.34"]});

  const validated = await policy.validateUrl("https://public.example/article?x=1");

  assert.equal(validated.address, "93.184.216.34");
  assert.equal(policy.isUrlAuthorized(validated.exactUrl), false);
  assert.equal(policy.isAuthorityAuthorized("https:", "public.example", 443), false);
  await policy.authorizeUrl(validated.exactUrl);
  assert.equal(policy.isUrlAuthorized(validated.exactUrl), true);
  assert.equal(policy.isAuthorityAuthorized("https:", "public.example", 443), true);
});


test("preauthorizes only the plan-bound first hop for proxy setup", async () => {
  const policy = new PublicNetworkPolicy({resolver: async () => ["93.184.216.34"]});

  const planned = await policy.authorizePlannedNavigation(
    "https://public.example/article?x=1#section",
  );

  assert.equal(policy.isUrlAuthorized(planned.exactUrl), false);
  assert.equal(policy.isProxyUrlAuthorized(planned.exactUrl), true);
  assert.equal(policy.isAuthorityAuthorized("https:", "public.example", 443), true);
  assert.equal(
    policy.isProxyUrlAuthorized("https://public.example/other?x=1"),
    false,
  );
});


test("treats URL fragments as local document state for exact network authorization", async () => {
  const policy = new PublicNetworkPolicy({resolver: async () => ["93.184.216.34"]});

  await policy.authorizeUrl("https://public.example/article?x=1");

  assert.equal(
    policy.isUrlAuthorized("https://public.example/article?x=1#section-2"),
    true,
  );
  assert.equal(
    policy.isUrlAuthorized("https://public.example/article?x=2#section-2"),
    false,
  );
});


test("rejects a hostname when any DNS answer is non-public", async () => {
  const policy = new PublicNetworkPolicy({
    resolver: async () => ["93.184.216.34", "127.0.0.1"],
  });

  await assert.rejects(
    policy.authorizeUrl("https://mixed.example/article"),
    /unsafe_url/,
  );
  assert.equal(policy.isAuthorityAuthorized("https:", "mixed.example", 443), false);
});


test("rejects non-HTTP schemes before DNS resolution", async () => {
  let resolverCalled = false;
  const policy = new PublicNetworkPolicy({
    resolver: async () => {
      resolverCalled = true;
      return ["93.184.216.34"];
    },
  });

  await assert.rejects(
    policy.authorizeUrl("file:///Users/example/secret"),
    /unsafe_url/,
  );
  assert.equal(resolverCalled, false);
});


test("rejects IPv6 transition and benchmark ranges that can evade IPv4 policy", async () => {
  const policy = new PublicNetworkPolicy({resolver: async () => ["93.184.216.34"]});

  for (const address of [
    "::7f00:1",
    "64:ff9b::7f00:1",
    "2002:7f00:1::",
    "2001:2::1",
    "4000::1",
  ]) {
    await assert.rejects(
      policy.authorizeUrl(`http://[${address}]/context`),
      /unsafe_url/,
      address,
    );
  }

  for (const address of ["192.0.0.9", "192.0.0.10"]) {
    await assert.rejects(
      policy.authorizeUrl(`http://${address}/context`),
      /unsafe_url/,
      address,
    );
  }
});


test("rejects an unbounded DNS answer set", async () => {
  const policy = new PublicNetworkPolicy({
    resolver: async () => Array.from({length: 65}, () => "93.184.216.34"),
  });

  await assert.rejects(
    policy.authorizeUrl("https://many-answers.example/context"),
    /unsafe_url/,
  );
});


test("cancels system DNS resolution at the caller deadline", async () => {
  let rejectA;
  let rejectAaaa;
  const resolver = {
    resolve4: () => new Promise((_resolve, reject) => { rejectA = reject; }),
    resolve6: () => new Promise((_resolve, reject) => { rejectAaaa = reject; }),
    cancel() {
      const error = Object.assign(new Error("cancelled"), {code: "ECANCELLED"});
      rejectA(error);
      rejectAaaa(error);
    },
  };
  const started = Date.now();

  await assert.rejects(
    resolveSystemHostname("public.example", {
      deadline: Date.now() + 30,
      resolverFactory: () => resolver,
    }),
    /unsafe_url/,
  );
  assert.ok(Date.now() - started < 250);
});


test("accepts one public DNS family when the other has no records", async () => {
  const noData = Object.assign(new Error("no data"), {code: "ENODATA"});
  const addresses = await resolveSystemHostname("public.example", {
    deadline: Date.now() + 1000,
    resolverFactory: () => ({
      resolve4: async () => ["93.184.216.34"],
      resolve6: async () => { throw noData; },
      cancel() {},
    }),
  });

  assert.deepEqual(addresses, ["93.184.216.34"]);
});
