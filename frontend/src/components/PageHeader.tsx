import type { ReactNode } from "react";
export function PageHeader({ title, description, action }: { title: string; description?: string; action?: ReactNode }) {
  return <div className="pageHeader"><div><h1>{title}</h1>{description && <p>{description}</p>}</div>{action && <div className="pageHeaderAction">{action}</div>}</div>;
}
