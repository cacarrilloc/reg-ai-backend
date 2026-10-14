-- phase_43: persist LLM feed summaries for regulatory publications

alter table public.regulatory_publications
  add column if not exists feed_summary text;

comment on column public.regulatory_publications.feed_summary is
  'LLM reader summary for the Regulatory Feed table (~1000 characters)';
