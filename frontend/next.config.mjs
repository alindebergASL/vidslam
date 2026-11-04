/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  images: { unoptimized: true },
  // (Removed experimental.serverActions.allowedOrigins: ["*"]: the app has no
  // server actions, and a wildcard origin list only widened the surface.)
};
export default nextConfig;
