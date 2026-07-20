import dns from "node:dns/promises";
import net from "node:net";


const nonPublicIpv4s = new net.BlockList();
for (const [network, prefix] of [
  ["0.0.0.0", 8],
  ["10.0.0.0", 8],
  ["100.64.0.0", 10],
  ["127.0.0.0", 8],
  ["169.254.0.0", 16],
  ["172.16.0.0", 12],
  ["192.0.0.0", 24],
  ["192.0.2.0", 24],
  ["192.168.0.0", 16],
  ["198.18.0.0", 15],
  ["198.51.100.0", 24],
  ["203.0.113.0", 24],
  ["224.0.0.0", 4],
  ["240.0.0.0", 4],
]) {
  nonPublicIpv4s.addSubnet(network, prefix, "ipv4");
}

const nonPublicIpv6s = new net.BlockList();
for (const [network, prefix] of [
  ["::", 128],
  ["::1", 128],
  ["::ffff:0:0", 96],
  ["64:ff9b:1::", 48],
  ["100::", 64],
  ["2001::", 23],
  ["2001:2::", 48],
  ["2001:db8::", 32],
  ["2002::", 16],
  ["3ffe::", 16],
  ["3fff::", 20],
  ["5f00::", 16],
  ["fc00::", 7],
  ["fe80::", 10],
  ["fec0::", 10],
  ["ff00::", 8],
]) {
  nonPublicIpv6s.addSubnet(network, prefix, "ipv6");
}
const globalUnicastIpv6s = new net.BlockList();
globalUnicastIpv6s.addSubnet("2000::", 3, "ipv6");


export function isPublicIp(value) {
  const family = net.isIP(value);
  if (family === 4) {
    return !nonPublicIpv4s.check(value, "ipv4");
  }
  if (family === 6) {
    return (
      globalUnicastIpv6s.check(value, "ipv6") &&
      !nonPublicIpv6s.check(value, "ipv6")
    );
  }
  return false;
}


function normalizeHostname(parsed) {
  let hostname = parsed.hostname.replace(/\.$/, "").toLowerCase();
  if (hostname.startsWith("[") && hostname.endsWith("]")) {
    hostname = hostname.slice(1, -1);
  }
  return hostname;
}


export function parsePublicHttpUrl(value) {
  if (typeof value !== "string" || value.length === 0 || value.length > 4096) {
    throw new Error("unsafe_url");
  }
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error("unsafe_url");
  }
  const hostname = normalizeHostname(parsed);
  if (
    !["http:", "https:"].includes(parsed.protocol) ||
    parsed.username ||
    parsed.password ||
    !hostname ||
    hostname === "localhost" ||
    hostname.endsWith(".localhost") ||
    hostname.endsWith(".local")
  ) {
    throw new Error("unsafe_url");
  }
  if (net.isIP(hostname) && !isPublicIp(hostname)) {
    throw new Error("unsafe_url");
  }
  const port = parsed.port
    ? Number.parseInt(parsed.port, 10)
    : (parsed.protocol === "https:" ? 443 : 80);
  if (!Number.isSafeInteger(port) || port < 1 || port > 65535) {
    throw new Error("unsafe_url");
  }
  return { parsed, hostname, port };
}


export async function resolveSystemHostname(
  hostname,
  {
    deadline,
    resolverFactory = () => new dns.Resolver(),
  } = {},
) {
  const remaining = deadline - Date.now();
  if (!Number.isFinite(deadline) || remaining <= 0) {
    throw new Error("unsafe_url");
  }
  const resolver = resolverFactory();
  if (
    !resolver ||
    typeof resolver.resolve4 !== "function" ||
    typeof resolver.resolve6 !== "function" ||
    typeof resolver.cancel !== "function"
  ) {
    throw new TypeError("invalid DNS resolver");
  }
  const allowNoRecords = async (operation) => {
    try {
      return await operation();
    } catch (error) {
      if (["ENODATA", "ENOTFOUND"].includes(error?.code)) {
        return [];
      }
      throw error;
    }
  };
  const timer = setTimeout(() => {
    try {
      resolver.cancel();
    } catch {
      // The pending family promises still determine the fail-closed result.
    }
  }, remaining);
  try {
    const addresses = (
      await Promise.all([
        allowNoRecords(() => resolver.resolve4(hostname)),
        allowNoRecords(() => resolver.resolve6(hostname)),
      ])
    ).flat();
    if (addresses.length === 0) {
      throw new Error("unsafe_url");
    }
    return addresses;
  } catch {
    throw new Error("unsafe_url");
  } finally {
    clearTimeout(timer);
  }
}


function authorityKey(protocol, hostname, port) {
  return `${protocol}//${hostname}:${port}`;
}


function networkUrlKey(value) {
  const parsed = new URL(value);
  parsed.hash = "";
  return parsed.href;
}


export class PublicNetworkPolicy {
  constructor({ resolver }) {
    if (typeof resolver !== "function") {
      throw new TypeError("resolver must be a function");
    }
    this.resolver = resolver;
    this.pins = new Map();
    this.authorities = new Map();
    this.urls = new Set();
    this.proxyUrls = new Set();
  }

  async validateUrl(value) {
    const { parsed, hostname, port } = parsePublicHttpUrl(value);
    let pin = this.pins.get(hostname);
    if (!pin) {
      const resolved = net.isIP(hostname) ? [hostname] : await this.resolver(hostname);
      const addresses = Array.isArray(resolved)
        ? resolved.map((item) => typeof item === "string" ? item : item?.address)
        : [];
      if (
        addresses.length === 0 ||
        addresses.length > 64 ||
        addresses.some((address) => typeof address !== "string" || !isPublicIp(address))
      ) {
        throw new Error("unsafe_url");
      }
      pin = Object.freeze({ hostname, address: addresses[0], addresses: Object.freeze([...addresses]) });
      this.pins.set(hostname, pin);
    }
    const exactUrl = parsed.href;
    return Object.freeze({
      exactUrl,
      protocol: parsed.protocol,
      hostname,
      port,
      address: pin.address,
      addresses: pin.addresses,
    });
  }

  async authorizePlannedNavigation(value) {
    const authorization = await this.validateUrl(value);
    this.proxyUrls.add(networkUrlKey(authorization.exactUrl));
    this.authorities.set(
      authorityKey(
        authorization.protocol,
        authorization.hostname,
        authorization.port,
      ),
      authorization,
    );
    return authorization;
  }

  async authorizeUrl(value) {
    const authorization = await this.authorizePlannedNavigation(value);
    // Fragments are local document state and never cross the HTTP boundary.
    // Fetch authorization remains distinct from the proxy-only first-hop grant.
    this.urls.add(networkUrlKey(authorization.exactUrl));
    return authorization;
  }

  isUrlAuthorized(value) {
    try {
      return this.urls.has(networkUrlKey(value));
    } catch {
      return false;
    }
  }

  isProxyUrlAuthorized(value) {
    try {
      return this.proxyUrls.has(networkUrlKey(value));
    } catch {
      return false;
    }
  }

  isAuthorityAuthorized(protocol, hostname, port) {
    const normalized = String(hostname || "").replace(/\.$/, "").toLowerCase();
    return this.authorities.has(authorityKey(protocol, normalized, port));
  }

  authorizationForAuthority(protocol, hostname, port) {
    const normalized = String(hostname || "").replace(/\.$/, "").toLowerCase();
    return this.authorities.get(authorityKey(protocol, normalized, port)) || null;
  }
}
