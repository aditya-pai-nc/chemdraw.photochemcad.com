/** @type {import('next').NextConfig} */
const nextConfig = {
  // Static export: `next build` emits plain HTML/JS into ./out, which the
  // FastAPI backend serves the same way it used to serve Vite's ./dist.
  // Node is a build-time dependency only — nothing runs it in production.
  output: 'export',
  images: { unoptimized: true },
  reactStrictMode: true
}

export default nextConfig
