import type { Metadata } from "next";
import "./globals.css";
export const metadata: Metadata = { title: "Marketplace Hub", description: "Marketplace operations platform" };
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) { return <html lang="it"><body>{children}</body></html>; }
