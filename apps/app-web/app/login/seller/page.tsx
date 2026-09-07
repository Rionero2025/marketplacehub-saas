import Link from "next/link";
import { LoginForm } from "../../components/LoginForm";

export default function SellerLogin() {
  return <><LoginForm realm="seller" title="Accesso Seller" description="Entra nel pannello operativo del tuo negozio." /><Link className="back-link" href="/">Torna alla scelta del portale</Link></>;
}
