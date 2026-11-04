import { OwnerConsole } from "@/components/OwnerConsole";

export default function OwnerPage() {
  return (
    <>
      <h1>Owner console</h1>
      <p className="lede">Fund your buyer agent, set the rules it must follow, and take money back. Your wallet signs every change.</p>
      <div style={{ marginTop: "2rem" }}><OwnerConsole /></div>
    </>
  );
}
