import type { Metadata } from "next";
import type { ReactNode } from "react";

export const metadata: Metadata = {
  title: "Accesso Platform | Marketplace Hub",
  robots: { index: false, follow: false },
};

export default function PlatformLoginLayout({ children }: { children: ReactNode }) {
  return children;
}
