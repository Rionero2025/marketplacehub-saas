"use client";
import { WorkspaceProvider } from "./WorkspaceProvider";
import { AppShell } from "./AppShell";
export function WorkspacePage({ children }: { children: React.ReactNode }) { return <WorkspaceProvider><AppShell>{children}</AppShell></WorkspaceProvider>; }
