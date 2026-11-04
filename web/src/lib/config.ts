// Public settings compiled into the static site. Never put secrets here.
import { DEFAULT_TESTNET_RPCS, makeChain } from "./near.mjs";

export const NETWORK = (process.env.NEXT_PUBLIC_NETWORK || "testnet") as "testnet" | "mainnet";
export const CONTRACT_ID = process.env.NEXT_PUBLIC_CONTRACT_ID || "kavren.lahd-kavren.testnet";
export const DEFAULT_AGENT = process.env.NEXT_PUBLIC_DEFAULT_AGENT || "buyer-agent.lahd-kavren.testnet";
// "same-origin" when the site is served by the API service itself (the Render setup);
// a URL for local review; empty to leave the API out entirely.
const api = (process.env.NEXT_PUBLIC_API_URL || "").trim();
export const API_ENABLED = api !== "";
export const API_URL = api === "same-origin" ? "" : api.replace(/\/$/, "");
const custom = (process.env.NEXT_PUBLIC_RPC_URLS || "").split(",").map((s) => s.trim()).filter(Boolean);
export const RPCS: string[] = custom.length ? custom : DEFAULT_TESTNET_RPCS;
export const EXPLORER = NETWORK === "testnet" ? "https://testnet.nearblocks.io" : "https://nearblocks.io";

export const chain = makeChain({ contractId: CONTRACT_ID, rpcs: RPCS });

export const accountUrl = (a: string) => `${EXPLORER}/address/${a}`;
export const txUrl = (h: string) => `${EXPLORER}/txns/${h}`;
