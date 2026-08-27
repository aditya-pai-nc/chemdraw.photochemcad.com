/** @type {import('next').NextConfig} */
const nextConfig = {
  // Runs as a Node server (`next start`), not a static export: the browser
  // talks only to Next, and app/api/[...path] proxies to FastAPI server-side.
  // Route handlers cannot exist in an `output: 'export'` build.
  reactStrictMode: true,
  // Uploads pass straight through the proxy as a stream, but Next still caps
  // the body it will accept for a route handler.
  experimental: {
    proxyTimeout: 1000 * 60 * 30
  }
}

export default nextConfig
