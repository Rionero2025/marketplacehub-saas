"use client";
import type { ReactNode } from "react";
import { WorkspaceProvider } from "./WorkspaceProvider";
import { AppShell } from "./AppShell";
export function WorkspacePage({ children }: { children: ReactNode }) { return <WorkspaceProvider><AppShell>{children}</AppShell></WorkspaceProvider>; }
