// Injected into the immutable LMM store package with go test -overlay.
// Every Extore response comes from the native Rust HTTP service. The only
// transport substitution is an isolated, certificate-verified TLS bridge.
package store

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"math/big"
	"net"
	"net/http"
	"net/http/httptest"
	"net/http/httputil"
	"net/url"
	"os"
	"regexp"
	"strings"
	"testing"
	"time"

	"github.com/TokenNotIncluded/lmm-api/extensions/internal/extore"
)

func TestNativeRustExtoreConnection(t *testing.T) {
	var config struct {
		Origin      string `json:"origin"`
		NativeURL   string `json:"native_url"`
		OwnerCookie string `json:"owner_cookie"`
		ReportPath  string `json:"report_path"`
	}
	rawConfig, err := os.ReadFile(os.Getenv("EXTORE_NATIVE_PROBE_CONFIG"))
	must(t, err)
	must(t, json.Unmarshal(rawConfig, &config))
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()
	upstream, err := url.Parse(config.NativeURL)
	must(t, err)
	if upstream.Scheme != "http" || upstream.Hostname() != "127.0.0.1" {
		t.Fatal("the native fixture must use loopback HTTP")
	}
	origin, err := url.Parse(config.Origin)
	must(t, err)
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	must(t, err)
	certificate := &x509.Certificate{
		SerialNumber: big.NewInt(time.Now().UnixNano()),
		Subject:      pkix.Name{CommonName: origin.Hostname()},
		DNSNames:     []string{origin.Hostname()},
		NotBefore:    time.Now().Add(-time.Minute), NotAfter: time.Now().Add(time.Hour),
		KeyUsage:    x509.KeyUsageDigitalSignature | x509.KeyUsageCertSign,
		ExtKeyUsage: []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		IsCA:        true, BasicConstraintsValid: true,
	}
	der, err := x509.CreateCertificate(rand.Reader, certificate, certificate, &key.PublicKey, key)
	must(t, err)
	keyDER, err := x509.MarshalECPrivateKey(key)
	must(t, err)
	pair, err := tls.X509KeyPair(pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}), pem.EncodeToMemory(&pem.Block{Type: "EC PRIVATE KEY", Bytes: keyDER}))
	must(t, err)
	bridge := httptest.NewUnstartedServer(httputil.NewSingleHostReverseProxy(upstream))
	bridge.TLS = &tls.Config{Certificates: []tls.Certificate{pair}, MinVersion: tls.VersionTLS12}
	bridge.StartTLS()
	defer bridge.Close()
	cert, err := x509.ParseCertificate(der)
	must(t, err)
	trust := x509.NewCertPool()
	trust.AddCert(cert)
	transport := &http.Transport{
		TLSClientConfig: &tls.Config{RootCAs: trust, MinVersion: tls.VersionTLS12},
		DialContext: func(ctx context.Context, network, address string) (net.Conn, error) {
			if address != origin.Hostname()+":443" {
				return nil, fmt.Errorf("unexpected test destination")
			}
			return (&net.Dialer{Timeout: 5 * time.Second}).DialContext(ctx, network, bridge.Listener.Addr().String())
		},
	}
	defer transport.CloseIdleConnections()
	httpClient := &http.Client{Transport: transport, Timeout: 10 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	api := func(method, path string, body any, owner bool, want int) ([]byte, http.Header) {
		t.Helper()
		var reader io.Reader
		if body != nil {
			encoded, err := json.Marshal(body)
			must(t, err)
			reader = bytes.NewReader(encoded)
		}
		request, err := http.NewRequestWithContext(ctx, method, config.Origin+path, reader)
		must(t, err)
		if owner {
			request.Header.Set("Cookie", "extore_session="+config.OwnerCookie)
			request.Header.Set("Origin", config.Origin)
		}
		if body != nil {
			request.Header.Set("Content-Type", "application/json")
		}
		response, err := httpClient.Do(request)
		must(t, err)
		defer response.Body.Close()
		data, err := io.ReadAll(io.LimitReader(response.Body, 16<<20))
		must(t, err)
		if response.StatusCode != want {
			t.Fatalf("%s %s returned HTTP %d; expected %d", method, request.URL.Path, response.StatusCode, want)
		}
		return data, response.Header
	}
	decode := func(data []byte) map[string]any {
		t.Helper()
		var value map[string]any
		must(t, json.Unmarshal(data, &value))
		return value
	}
	first := func(method, path string, body any, owner bool, want int) []byte {
		data, _ := api(method, path, body, owner, want)
		return data
	}
	health := decode(first("GET", "/health", nil, false, 200))
	if health["runtime"] != "rust" {
		t.Fatal("fixture is not the Rust runtime")
	}

	const privateBody = "NATIVE-SDK-FIXTURE-PRIVATE-BODY"
	const updatedBody = "NATIVE-SDK-FIXTURE-UPDATED-BODY"
	const callback = "https://native.api.lmm.best/store/manage"
	variants := []any{
		map[string]any{"id": "fixed", "name": "Fixed delivery", "price": "2.500000", "currency": "CNY", "enabled": true, "marketplace": map[string]any{"template": "fixed-content", "price_quota": int64(750000), "fixed_content": privateBody}},
		map[string]any{"id": "card", "name": "Card delivery", "price": nil, "currency": "CNY", "enabled": true, "marketplace": map[string]any{"template": "card-key", "price_quota": int64(500000), "fixed_content": nil}},
	}
	productInput := map[string]any{
		"name": "Native integration product", "description": "Created only in an isolated test database.",
		"public": true, "parameters": []any{}, "mode": "manual", "delivery": "content", "view_policy": "repeat",
		"variants":    variants,
		"marketplace": map[string]any{"title": "Native store template", "description": "Approved private marketplace configuration", "image_urls": []string{}, "links": []any{}, "payment_methods": []string{"balance"}},
	}
	created := decode(first("POST", "/api/admin/products", productInput, true, 200))
	pid, _ := created["id"].(string)
	if pid == "" {
		t.Fatal("product creation returned no identity")
	}
	// A second product proves the grant, rather than shop membership, limits reads.
	first("POST", "/api/admin/products", map[string]any{"name": "Unapproved source", "parameters": []any{}}, true, 200)
	public := first("GET", "/api/products", nil, false, 200)
	if !bytes.Contains(public, []byte(pid)) || bytes.Contains(public, []byte(privateBody)) || bytes.Contains(public, []byte("marketplace")) {
		t.Fatal("public product projection exposed private marketplace configuration")
	}

	sdk := extore.Client{HTTP: httpClient}
	must(t, sdk.Discover(ctx, config.Origin))
	flow, err := extore.NewFlow(config.Origin, "lmm-best-store", callback)
	must(t, err)
	state := "native-cross-stack-random-state-2026"
	authorize, err := url.Parse(flow.SyncAuthorizationURL(state))
	must(t, err)
	if authorize.Query().Get("scope") != "products.read marketplace.sync" || authorize.Query().Get("code_challenge_method") != "S256" || strings.Contains(authorize.String(), flow.Verifier) {
		t.Fatal("unexpected frozen SDK authorization contract")
	}
	_, headers := api("GET", authorize.RequestURI(), nil, false, 303)
	consent, err := url.Parse(headers.Get("Location"))
	must(t, err)
	if consent.Fragment == "" {
		t.Fatal("authorization did not create an explicit consent request")
	}
	review := decode(first("GET", "/api/admin/commerce/requests/"+consent.Fragment, nil, true, 200))
	approved := decode(first("POST", "/api/admin/commerce/requests/"+consent.Fragment+"/approve", map[string]any{
		"shop_id": review["shop"].(map[string]any)["id"], "review_digest": review["review_digest"],
		"scopes": []string{"products.read", "marketplace.sync"}, "product_ids": []string{pid},
		"card_limits": []any{}, "grant_expires": time.Now().Unix() + 3600,
	}, true, 200))
	callbackURL, _ := approved["redirect_uri"].(string)
	parsed, query, err := extore.ParseCallback(callbackURL)
	must(t, err)
	if parsed.Scheme+"://"+parsed.Host+parsed.Path != callback || query.Get("state") != state || query.Get("iss") != config.Origin {
		t.Fatal("native callback address, issuer, or state changed")
	}
	code, err := flow.ValidateCallback(callbackURL)
	must(t, err)
	credentials, err := sdk.Connect(ctx, flow, code)
	must(t, err)
	if credentials.Scope != "products.read marketplace.sync" {
		t.Fatal("unexpected issuance authority")
	}

	readListing := func(credentials extore.Credentials) (*extore.Catalog, json.RawMessage, map[string]any) {
		t.Helper()
		catalog, err := sdk.ConnectionCatalog(ctx, config.Origin, credentials)
		must(t, err)
		if len(catalog.Products) != 1 {
			t.Fatal("catalog escaped the approved product set")
		}
		listing := decode(catalog.Products[0])
		if listing["id"] != pid || !regexp.MustCompile(`^[a-f0-9]{64}$`).MatchString(fmt.Sprint(listing["sync_revision"])) {
			t.Fatal("native catalog identity or sync revision is invalid")
		}
		return catalog, catalog.Products[0], listing
	}
	catalog, raw, listing := readListing(*credentials)
	firstRevision := listing["sync_revision"].(string)
	firstPublicRevision := listing["revision"].(string)
	if firstRevision == firstPublicRevision || !bytes.Contains(raw, []byte(privateBody)) {
		t.Fatal("approved marketplace scope did not include its private template")
	}
	if listing["card_summary"].(map[string]any)["total"] != float64(0) {
		t.Fatal("fixture unexpectedly contains issued cards")
	}

	// Exercise the exact frozen store mapper and its transactional repository.
	service, _ := extoreService(t)
	const userID int64 = 1
	connectionID := ID(extorePurpose, "connection", userID, config.Origin, catalog.Shop.ID)
	secret, err := json.Marshal(credentials)
	must(t, err)
	ciphertext, err := service.extore.seal(extoreConnectionAAD(connectionID), secret)
	must(t, err)
	connection := extoreConnectionRow{ID: connectionID, Origin: config.Origin, ShopID: catalog.Shop.ID, ShopName: catalog.Shop.Name, ClientID: flow.ClientID, Ciphertext: ciphertext, ExpiresAt: int64(credentials.GrantExpires), Version: 1, UpdatedAt: time.Now().Unix()}
	must(t, service.repo.Within(ctx, extoreSellerShop(userID), func(tx Tx) error { return tx.Put(extoreConnectionKind, connectionID, connection) }))
	source, err := service.syncExtoreListing(ctx, userID, connectionID, raw)
	must(t, err)
	if source.Revision != firstRevision || source.ProductID == "" || len(source.VariantIDs) != 2 {
		t.Fatal("frozen LMM mapper did not preserve sync revision and variant identities")
	}
	localProductID := source.ProductID
	localFixedID := source.VariantIDs["fixed"]
	localCardID := source.VariantIDs["card"]
	localShop, err := service.legacyShop(ctx, Account{Kind: "personal", ID: userID})
	must(t, err)
	assertStored := func(title, body string, price int64) {
		t.Helper()
		var p Product
		var v Variant
		must(t, service.repo.Within(ctx, localShop.ID, func(tx Tx) error {
			if err := tx.Get("products", localProductID, &p); err != nil {
				return err
			}
			return tx.Get("variants", localFixedID, &v)
		}))
		if p.Title != title || p.Published || p.Legacy == nil || p.Legacy.Status != "draft" || v.Legacy == nil || v.Legacy.Template != "fixed-content" || v.Legacy.FixedContent != body || v.Price.Amount != price || v.Price.Unit != "credits" {
			t.Fatal("actual LMM persistence changed private template, sale price, or draft status")
		}
	}
	assertStored("Native store template", privateBody, 750000)

	// The connection cannot issue cards; the isolated merchant deliberately does.
	deniedRequest, err := http.NewRequestWithContext(ctx, "POST", config.Origin+"/api/integrations/commerce/cards", strings.NewReader(fmt.Sprintf(`{"product_id":%q,"variant_id":"card","count":1}`, pid)))
	must(t, err)
	deniedRequest.Header.Set("Authorization", "Bearer "+credentials.Access)
	deniedRequest.Header.Set("Content-Type", "application/json")
	deniedRequest.Header.Set("Idempotency-Key", "native-read-only-probe")
	denied, err := httpClient.Do(deniedRequest)
	must(t, err)
	denied.Body.Close()
	if denied.StatusCode != 403 {
		t.Fatal("read/sync grant acquired card issuance authority")
	}
	issued := decode(first("POST", "/api/admin/cards", map[string]any{"product_id": pid, "variant_id": "card", "count": 2}, true, 200))
	if len(issued["codes"].([]any)) != 2 {
		t.Fatal("isolated inventory mutation failed")
	}
	_, inventoryRaw, inventoryListing := readListing(*credentials)
	if inventoryListing["sync_revision"] != firstRevision || inventoryListing["card_summary"].(map[string]any)["total"] != float64(2) {
		t.Fatal("stock changes altered sync revision or were absent from the live summary")
	}
	source, err = service.syncExtoreListing(ctx, userID, connectionID, inventoryRaw)
	must(t, err)
	if source.ProductID != localProductID || source.VariantIDs["fixed"] != localFixedID || source.VariantIDs["card"] != localCardID || decode(source.CardSummary)["total"] != float64(2) {
		t.Fatal("inventory refresh recreated LMM products or missed updated stock summary")
	}
	assertStored("Native store template", privateBody, 750000)

	productInput["marketplace"].(map[string]any)["title"] = "Native store template updated"
	variants[0].(map[string]any)["marketplace"].(map[string]any)["fixed_content"] = updatedBody
	variants[0].(map[string]any)["marketplace"].(map[string]any)["price_quota"] = int64(900000)
	first("PUT", "/api/admin/products/"+pid, productInput, true, 200)
	_, changedRaw, changedListing := readListing(*credentials)
	if changedListing["sync_revision"] == firstRevision || changedListing["revision"] != firstPublicRevision {
		t.Fatal("private marketplace metadata must change only its sync revision")
	}
	source, err = service.syncExtoreListing(ctx, userID, connectionID, changedRaw)
	must(t, err)
	if source.ProductID != localProductID || source.VariantIDs["fixed"] != localFixedID || source.Revision != changedListing["sync_revision"] {
		t.Fatal("metadata synchronization lost product identity or revision")
	}
	assertStored("Native store template updated", updatedBody, 900000)
	public = first("GET", "/api/products", nil, false, 200)
	if bytes.Contains(public, []byte(updatedBody)) || bytes.Contains(public, []byte(privateBody)) {
		t.Fatal("updated private content escaped the public catalog")
	}

	refreshed, err := sdk.RefreshConnection(ctx, config.Origin, flow.ClientID, *credentials)
	must(t, err)
	if refreshed.Refresh == credentials.Refresh || refreshed.GrantID != credentials.GrantID {
		t.Fatal("refresh rotation did not retain the approved grant")
	}
	readListing(*refreshed)
	must(t, sdk.RevokeConnection(ctx, config.Origin, flow.ClientID, *refreshed))
	if _, err := sdk.ConnectionCatalog(ctx, config.Origin, *refreshed); err == nil {
		t.Fatal("revoked connection still reads source listings")
	}

	report := map[string]any{
		"status": "passed", "runtime": "rust", "native_callback": callback,
		"scope": "products.read marketplace.sync", "approved_products": 1,
		"checks":                  []string{"verified TLS bridge", "frozen SDK discovery", "real S256 PKCE authorization and consent", "native callback issuer and state", "form-encoded token exchange", "approved product isolation", "private per-SKU template projection", "actual frozen LMM mapper and persistence", "Credits price retained without currency conversion", "draft product and stable variant identities", "no card issuance permission", "inventory refresh retains sync revision", "private metadata changes sync revision", "public catalog redaction", "refresh token rotation", "revocation blocks catalog"},
		"initial_sync_revision":   firstRevision,
		"inventory_sync_revision": inventoryListing["sync_revision"],
		"updated_sync_revision":   changedListing["sync_revision"],
		"inventory_before":        0, "inventory_after": 2,
		"secret_values_logged": false, "production_accounts_created": false,
	}
	encoded, err := json.MarshalIndent(report, "", "  ")
	must(t, err)
	must(t, os.WriteFile(config.ReportPath, encoded, 0600))
}
