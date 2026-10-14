-- phase_48: analysis and redlines can use different chat models.
-- generation_* stays the redline model. analysis_* is the judgment-heavy model
-- (gap analysis, compliance reports, business-change review).
-- Null analysis columns mean "use the redline model" (organizations that picked one chat model).
-- A BYOK org may omit the chat model when the customer only brought an embedding key.
-- Chat then stays on the Reg-AI default and no chat-provider row is invented.

alter table public.organization_ai_settings
    drop constraint if exists organization_ai_settings_byok_generation;

alter table public.organization_ai_settings
    add column if not exists analysis_provider text,
    add column if not exists analysis_model text,
    add column if not exists analysis_api_base text;

comment on column public.organization_ai_settings.generation_model is
  'Chat model for redlines (tracked-change drafts). Null in platform mode.';
comment on column public.organization_ai_settings.analysis_model is
  'Chat model for analysis. Null means use generation_model.';
