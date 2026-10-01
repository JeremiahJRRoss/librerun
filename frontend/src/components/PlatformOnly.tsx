/**
 * What a tenant admin sees where the platform operator's page or tab would
 * be (K9-07; L31, D29): why it is not theirs, instead of a red error or an
 * endless "Loading…". The gate is the server's (`require_platform_admin`
 * answers 403); this only explains the answer, a reason per page.
 */
export default function PlatformOnly({ reason }: { reason: string }) {
  return (
    <div
      data-testid="platform-only"
      className="rounded border border-slate-300 bg-white p-4 text-sm text-slate-700"
    >
      <strong>Platform operators only.</strong> {reason} Your admin account administers your
      tenant, not the deployment.
    </div>
  );
}
