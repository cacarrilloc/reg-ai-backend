# Custom Access Token Hook (sample)

Deploy this as a Supabase Edge Function and attach it as the **Custom Access Token**
Auth Hook so FastAPI receives `org_id` and `role` on every access token.

## Expected claims

- `org_id` — opaque tenant id (`organizations.auth_org_id`)
- `role` — membership role (`org_admin`, `chief_compliance_officer`, …)
- `app_metadata.active_org_id` / `app_metadata.role` — mirrors for clients

## Pseudocode

```ts
// Deno Edge Function sketch — wire to your control-plane DB credentials.
import { createClient } from "https://esm.sh/@supabase/supabase-js@2";

Deno.serve(async (req) => {
  const { user, claims } = await req.json();
  const admin = createClient(
    Deno.env.get("SUPABASE_URL")!,
    Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  );

  const activeOrgId =
    user.app_metadata?.active_org_id ??
    user.app_metadata?.org_id ??
    null;

  let role = user.app_metadata?.role ?? "org_viewer";
  if (activeOrgId) {
    const { data } = await admin
      .from("organization_members")
      .select("role")
      .eq("auth_org_id", activeOrgId)
      .eq("auth_user_id", user.id)
      .eq("status", "active")
      .maybeSingle();
    if (data?.role) role = data.role;
  }

  const app_metadata = {
    ...(claims.app_metadata || {}),
    active_org_id: activeOrgId,
    role,
  };

  return new Response(
    JSON.stringify({
      claims: {
        ...claims,
        org_id: activeOrgId,
        role,
        app_metadata,
      },
    }),
    { headers: { "Content-Type": "application/json" } },
  );
});
```

## Local / staging checklist

1. Apply `phase_25_supabase_auth_saas` migration (renames + new tables).
2. Deploy the hook and set secrets.
3. Sign in → create org → switch org → decode JWT and confirm `org_id`/`role`.
4. Call a protected FastAPI route with `Authorization: Bearer <access_token>`.
