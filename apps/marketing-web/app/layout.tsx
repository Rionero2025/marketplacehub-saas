import type { Metadata } from "next";
import type { ReactNode } from "react";
import "./styles.css";

export const metadata: Metadata = { title: "Marketplace Hub" };

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return <html lang="it"><body>{children}</body></html>;
}
