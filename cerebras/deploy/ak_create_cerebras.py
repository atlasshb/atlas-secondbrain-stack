# Runs inside `ak shell` on atlas-authentik-server. Idempotent.
# Creates the Cerebras proxy provider (forward_single) + application and
# attaches it to the embedded outpost — same shape as the 11 existing ones.
from authentik.core.models import Application
from authentik.flows.models import Flow
from authentik.outposts.models import Outpost
from authentik.providers.proxy.models import ProxyProvider

EXTERNAL_HOST = "https://cerebras-staging.your-domain.tld"  # go-live: update to cerebras.

auth_flow = Flow.objects.get(slug="default-provider-authorization-implicit-consent")
inval_flow = Flow.objects.get(slug="default-provider-invalidation-flow")

provider, created_p = ProxyProvider.objects.get_or_create(
    name="cerebras",
    defaults=dict(
        authorization_flow=auth_flow,
        invalidation_flow=inval_flow,
        external_host=EXTERNAL_HOST,
        mode="forward_single",
    ),
)
app, created_a = Application.objects.get_or_create(
    slug="cerebras",
    defaults=dict(name="Cerebras", provider=provider,
                  meta_description="Atlas ops cockpit (Pulse)"),
)
outpost = Outpost.objects.filter(managed="goauthentik.io/outposts/embedded").first()
outpost.providers.add(provider)
outpost.save()
print(f"provider pk={provider.pk} created={created_p} host={provider.external_host}")
print(f"application slug={app.slug} created={created_a}")
print(f"outpost '{outpost.name}' now serves {outpost.providers.count()} providers")
