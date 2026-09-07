import Link from "next/link";
import { LoginForm } from "../../components/LoginForm";

export default function AgencyLogin() {
  return <><LoginForm realm="agency" title="Accesso Agenzia" description="Entra nell’ambiente dedicato alla gestione dei tuoi Seller." /><Link className="back-link" href="/">Torna alla scelta del portale</Link></>;
}
