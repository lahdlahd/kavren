import type { Job } from "./near.mjs";
export type Tone = "pass" | "fail" | "wait" | "neutral";
export interface Seal { word: string; tone: Tone; line: string }
export interface TimelineEvent { when: number | null; title: string; detail: string; tone: Tone; data: Record<string, string | null> }
export function sealFor(job: Job, now: number): Seal;
export function buildTimeline(job: Job, now: number): TimelineEvent[];
export function attemptsLine(job: Job): string;
