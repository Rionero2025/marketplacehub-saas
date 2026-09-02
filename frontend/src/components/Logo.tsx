export function Logo({ compact = false }: { compact?: boolean }) {
  return <div className="brand"><span className="brandMark">MH</span>{!compact && <span>Marketplace Hub</span>}</div>;
}
