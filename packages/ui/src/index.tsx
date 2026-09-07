import type { ReactNode } from "react";

type FoundationStatusProps = {
  eyebrow: string;
  title: string;
  children: ReactNode;
};

export function FoundationStatus({ eyebrow, title, children }: FoundationStatusProps) {
  return (
    <main className="foundation-shell">
      <section className="foundation-card">
        <p className="foundation-eyebrow">{eyebrow}</p>
        <h1>{title}</h1>
        <div>{children}</div>
      </section>
    </main>
  );
}
