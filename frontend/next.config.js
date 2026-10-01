/** @type {import('next').NextConfig} */
const nextConfig = {
  output: "standalone",
  reactStrictMode: true,
  // Same-origin reverse proxy: when ``BACKEND_INTERNAL_URL`` is set, the
  // Next.js server forwards every ``/api/v1/*`` request to that URL on the
  // server side. Combined with ``NEXT_PUBLIC_API_URL=/api/v1`` baked into
  // the browser bundle (see ``compose.yaml`` build args + Dockerfile), the
  // visitor's browser only ever talks to the public origin — the backend
  // stays internal and CORS is sidestepped because requests are
  // same-origin.
  //
  // Leave ``BACKEND_INTERNAL_URL`` unset for direct browser→backend
  // deployments (the legacy default), in which case rewrites are a no-op
  // and ``NEXT_PUBLIC_API_URL`` must be a full reachable URL.
  async rewrites() {
    const backend = process.env.BACKEND_INTERNAL_URL;
    if (!backend) return [];
    return [
      {
        source: "/api/v1/:path*",
        destination: `${backend.replace(/\/$/, "")}/api/v1/:path*`,
      },
    ];
  },
};
module.exports = nextConfig;
