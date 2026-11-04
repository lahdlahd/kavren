"use client";
import { useEffect, useState } from "react";
import { chain } from "@/lib/config";
import type { Job } from "@/lib/near.mjs";

export type Load<T> = { state: "loading" } | { state: "error"; message: string } | { state: "ready"; value: T };

export function useAsync<T>(fn: () => Promise<T>, deps: unknown[]): Load<T> {
  const [s, set] = useState<Load<T>>({ state: "loading" });
  useEffect(() => {
    let live = true;
    set({ state: "loading" });
    fn().then((value) => live && set({ state: "ready", value }))
      .catch((e: Error) => live && set({ state: "error", message: e.message }));
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return s;
}

export const loadJob = (id: number) => chain.getJob(id) as Promise<Job | null>;
