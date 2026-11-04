/** Static export: the site is plain files, readable from any static host. It talks to
 * the NEAR RPC directly from the browser; the API is used only for the public canary
 * opening of a finished job. */
const config = {
  output: "export",
  images: { unoptimized: true },
  trailingSlash: true,
  reactStrictMode: true,
};
export default config;
