import { JobsList } from "@/components/JobsList";

export default function JobsPage() {
  return (
    <>
      <h1>Jobs</h1>
      <p className="lede">Every job on the contract, newest first, read from NEAR by your browser.</p>
      <JobsList />
    </>
  );
}
