/** @type {import('next').NextConfig} */
const securityHeaders = [
  { key: "X-Frame-Options", value: "DENY" },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "Referrer-Policy", value: "same-origin" },
];
// OMS_API_URL is read at runtime by the server-side proxy (app/api/oms); nothing about the OMS is baked into the
// browser bundle (remediation E1).
const nextConfig = {
  output: "standalone", reactStrictMode: true, poweredByHeader: false,
  async headers() { return [{ source: "/:path*", headers: securityHeaders }]; },
};
export default nextConfig;
