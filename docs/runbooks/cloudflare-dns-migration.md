# Cloudflare DNS migration for `goodwiinz.tech`

This runbook moves authoritative DNS hosting from Namify to Cloudflare. It
does not transfer the domain registration, change ACM certificates, or update
Terraform state.

## 1. Add the zone to Cloudflare

Operator action:

1. In the Cloudflare dashboard, select **Onboard a domain**, enter
   `goodwiinz.tech`, and choose the **Free** plan.
2. Use Cloudflare's automatic DNS record scan/import.
3. Review every imported record before changing nameservers. The scan is not
   guaranteed to find everything; preserve the current apex, application,
   mail (MX), TXT, CAA, and verification records.
4. Add or verify the ACM validation CNAME in the next section. Keep it
   **DNS only** (not proxied).
5. Copy the two nameservers assigned to this zone from its Overview page.

Cloudflare's current setup guide is
[Set up a primary zone](https://developers.cloudflare.com/dns/zone-setups/full-setup/setup/).

## 2. Preserve ACM validation

This record validates ACM certificate
`2fabe0a2-44f1-4e59-8160-69ccc5b4c8bb`. Keep or create it in the Cloudflare
zone before changing nameservers:

| Type | Name | Target | Proxy | TTL |
|---|---|---|---|---|
| CNAME | `_41b2837fa1362004d31e7fef630c608a.goodwiinz.tech` | `_6d3697fef8a5a12d78c62c43e007644d.wzccmgtwzk.acm-validations.aws.` | DNS only | Auto |

Cloudflare may display the name without the `goodwiinz.tech` suffix and may
omit the target's final dot after saving. Do not create a doubled name such as
`...goodwiinz.tech.goodwiinz.tech`.

## 3. Delegate the domain at Namify

Operator action:

1. If DNSSEC is enabled at Namify, remove the existing DS record before the
   nameserver change. Re-enable DNSSEC through Cloudflare after the zone is
   active.
2. In Namify's domain management for `goodwiinz.tech`, open the nameserver or
   DNS-hosting settings and choose custom nameservers.
3. Remove every current authoritative nameserver and enter exactly the two
   nameservers Cloudflare assigned in step 1.
4. Save the change. This changes DNS hosting only; the registration remains
   at Namify.
5. Wait for Cloudflare to show the zone as **Active**, then verify delegation:

```sh
dig +short NS goodwiinz.tech
```

The result must contain only the two assigned Cloudflare nameservers.

## 4. Create a scoped external-dns token

Operator action:

1. In Cloudflare, open **My Profile > API Tokens > Create Token**.
2. Start with **Edit zone DNS** or create a custom token.
3. Set the permission to **Zone > DNS > Edit**.
4. Under Zone Resources, choose **Include > Specific zone >
   `goodwiinz.tech`**. Do not grant all-zone or account-wide access.
5. Create the token and copy it immediately; Cloudflare displays the secret
   once. Store it in the approved secret manager, never in Git.

See Cloudflare's
[API token guide](https://developers.cloudflare.com/fundamentals/api/get-started/create-token/)
for the current dashboard flow.

## 5. Supply the token to external-dns

Read the token without putting it in shell history, update the existing
secret, clear the shell variable, and restart external-dns:

```sh
read -rsp "Cloudflare API token: " NOUS_CF_API_TOKEN && echo
eval "$(aws configure export-credentials --format env)" && \
kubectl create secret generic cloudflare-api-token -n kube-system \
  --from-literal=api-token="$NOUS_CF_API_TOKEN" \
  --save-config --dry-run=client -o yaml | kubectl apply -f -
unset NOUS_CF_API_TOKEN

eval "$(aws configure export-credentials --format env)" && \
kubectl rollout restart deployment/external-dns -n kube-system

eval "$(aws configure export-credentials --format env)" && \
kubectl get pods -n kube-system -l app.kubernetes.io/name=external-dns
```

Before the real token is supplied, `Error`/`CrashLoopBackOff` with Cloudflare
`Invalid format for Authorization header` is expected. Afterward, the pod
should become `Running` and external-dns should manage only
`goodwiinz.tech` because its deployment uses
`--domain-filter=goodwiinz.tech` and `--provider=cloudflare`.

## Fallback: keep DNS hosted at Namify

If the nameserver migration is cancelled, do not supply the Cloudflare token.
Create these records manually at Namify. The ALB hostname below was active on
2026-09-21; re-check it before editing DNS because an ALB replacement changes
the hostname.

| Purpose | Type/name | Target | Notes |
|---|---|---|---|
| ACM validation | CNAME `_41b2837fa1362004d31e7fef630c608a.goodwiinz.tech` | `_6d3697fef8a5a12d78c62c43e007644d.wzccmgtwzk.acm-validations.aws.` | Keep continuously; use DNS only if Namify offers proxying. |
| API | ALIAS or CNAME `dev-api.goodwiinz.tech` | `k8s-nousdev-dd1205cd33-608023008.us-east-1.elb.amazonaws.com.` | TTL 300; DNS only. |
| WebSocket | ALIAS or CNAME `dev-ws.goodwiinz.tech` | `k8s-nousdev-dd1205cd33-608023008.us-east-1.elb.amazonaws.com.` | TTL 300; DNS only. |

Confirm the current NOUS ALB before creating the application records:

```sh
eval "$(aws configure export-credentials --format env)" && \
aws elbv2 describe-load-balancers --region us-east-1 \
  --query "LoadBalancers[?contains(LoadBalancerName, 'nous')].[LoadBalancerName,DNSName,State.Code]" \
  --output table
```

Disable the Cloudflare-only controller while Namify remains authoritative so
it cannot fight the manual records:

```sh
eval "$(aws configure export-credentials --format env)" && \
kubectl scale deployment/external-dns -n kube-system --replicas=0
```

Re-enable it only after Cloudflare is authoritative and the scoped token is in
place:

```sh
eval "$(aws configure export-credentials --format env)" && \
kubectl scale deployment/external-dns -n kube-system --replicas=1
```
