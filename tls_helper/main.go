package main

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"net"
	"net/http"
	"net/url"
	"strings"
	"time"

	fhttp "github.com/bogdanfinn/fhttp"
	tls_client "github.com/bogdanfinn/tls-client"
	"github.com/bogdanfinn/tls-client/profiles"
)

type requestPayload struct {
	Method     string     `json:"method"`
	URL        string     `json:"url"`
	Headers    [][]string `json:"headers"`
	BodyBase64 string     `json:"body_base64"`
	Proxy      string     `json:"proxy"`
	TimeoutSec int        `json:"timeout_sec"`
	Profile    string     `json:"profile"`
}

type responsePayload struct {
	Status     int    `json:"status"`
	BodyBase64 string `json:"body_base64"`
}

func profileSpec(name string) profiles.ClientProfile {
	switch strings.ToLower(strings.TrimSpace(name)) {
	case "chrome_120", "chrome120":
		return profiles.Chrome_120
	case "chrome_124", "chrome124":
		return profiles.Chrome_124
	case "chrome_131", "chrome131", "", "chrome":
		return profiles.Chrome_131
	default:
		return profiles.Chrome_131
	}
}

func allowedGeminiURL(raw string) error {
	u, err := url.Parse(raw)
	if err != nil {
		return err
	}
	if u.Scheme != "https" || strings.ToLower(u.Hostname()) != "gemini.google.com" {
		return errors.New("only https://gemini.google.com URLs are allowed")
	}
	return nil
}

func timeoutSeconds(v int) int {
	if v <= 0 {
		return 180
	}
	if v > 300 {
		return 300
	}
	return v
}

func decodePayload(r *http.Request) (*requestPayload, []byte, error) {
	defer r.Body.Close()
	var p requestPayload
	if err := json.NewDecoder(io.LimitReader(r.Body, 64*1024*1024)).Decode(&p); err != nil {
		return nil, nil, err
	}
	if p.Method == "" {
		p.Method = "POST"
	}
	if err := allowedGeminiURL(p.URL); err != nil {
		return nil, nil, err
	}
	body, err := base64.StdEncoding.DecodeString(p.BodyBase64)
	if err != nil {
		return nil, nil, err
	}
	return &p, body, nil
}

func newClient(p *requestPayload) (tls_client.HttpClient, error) {
	jar := tls_client.NewCookieJar()
	options := []tls_client.HttpClientOption{
		tls_client.WithTimeoutSeconds(timeoutSeconds(p.TimeoutSec)),
		tls_client.WithClientProfile(profileSpec(p.Profile)),
		tls_client.WithCookieJar(jar),
		tls_client.WithNotFollowRedirects(),
	}
	if strings.TrimSpace(p.Proxy) != "" {
		options = append(options, tls_client.WithProxyUrl(p.Proxy))
	}
	client, err := tls_client.NewHttpClient(tls_client.NewNoopLogger(), options...)
	if err != nil {
		return nil, err
	}
	return client, nil
}

func buildRequest(p *requestPayload, body []byte) (*fhttp.Request, error) {
	req, err := fhttp.NewRequest(strings.ToUpper(p.Method), p.URL, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	headerOrder := make([]string, 0, len(p.Headers))
	for _, pair := range p.Headers {
		if len(pair) != 2 || pair[0] == "" {
			continue
		}
		req.Header.Add(pair[0], pair[1])
		headerOrder = append(headerOrder, strings.ToLower(pair[0]))
	}
	if len(headerOrder) > 0 {
		req.Header[fhttp.HeaderOrderKey] = headerOrder
		req.Header[fhttp.PHeaderOrderKey] = []string{":method", ":authority", ":scheme", ":path"}
	}
	return req, nil
}

func bearerOK(r *http.Request, secret string) bool {
	if secret == "" {
		return false
	}
	return r.Header.Get("Authorization") == "Bearer "+secret
}

func writeJSONError(w http.ResponseWriter, status int, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(map[string]string{"error": msg})
}

func requestHandler(secret string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeJSONError(w, http.StatusMethodNotAllowed, "method not allowed")
			return
		}
		if !bearerOK(r, secret) {
			writeJSONError(w, http.StatusUnauthorized, "unauthorized")
			return
		}
		p, body, err := decodePayload(r)
		if err != nil {
			writeJSONError(w, http.StatusBadRequest, err.Error())
			return
		}
		client, err := newClient(p)
		if err != nil {
			writeJSONError(w, http.StatusBadGateway, err.Error())
			return
		}
		defer client.CloseIdleConnections()
		req, err := buildRequest(p, body)
		if err != nil {
			writeJSONError(w, http.StatusBadRequest, err.Error())
			return
		}
		resp, err := client.Do(req)
		if err != nil {
			writeJSONError(w, http.StatusBadGateway, err.Error())
			return
		}
		defer resp.Body.Close()
		respBody, err := io.ReadAll(io.LimitReader(resp.Body, 64*1024*1024))
		if err != nil {
			writeJSONError(w, http.StatusBadGateway, err.Error())
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(responsePayload{Status: resp.StatusCode, BodyBase64: base64.StdEncoding.EncodeToString(respBody)})
	}
}

func streamHandler(secret string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeJSONError(w, http.StatusMethodNotAllowed, "method not allowed")
			return
		}
		if !bearerOK(r, secret) {
			writeJSONError(w, http.StatusUnauthorized, "unauthorized")
			return
		}
		p, body, err := decodePayload(r)
		if err != nil {
			writeJSONError(w, http.StatusBadRequest, err.Error())
			return
		}
		client, err := newClient(p)
		if err != nil {
			writeJSONError(w, http.StatusBadGateway, err.Error())
			return
		}
		defer client.CloseIdleConnections()
		req, err := buildRequest(p, body)
		if err != nil {
			writeJSONError(w, http.StatusBadRequest, err.Error())
			return
		}
		resp, err := client.Do(req)
		if err != nil {
			writeJSONError(w, http.StatusBadGateway, err.Error())
			return
		}
		defer resp.Body.Close()
		buf := make([]byte, 16*1024)
		firstN, firstErr := resp.Body.Read(buf)
		if firstN == 0 && firstErr != nil && firstErr != io.EOF {
			writeJSONError(w, http.StatusBadGateway, firstErr.Error())
			return
		}

		for k, values := range resp.Header {
			lk := strings.ToLower(k)
			if lk == "content-length" || lk == "content-encoding" || lk == "transfer-encoding" {
				continue
			}
			for _, v := range values {
				w.Header().Add(k, v)
			}
		}
		w.WriteHeader(resp.StatusCode)
		flusher, _ := w.(http.Flusher)
		if firstN > 0 {
			_, _ = w.Write(buf[:firstN])
			if flusher != nil {
				flusher.Flush()
			}
		}
		if firstErr == io.EOF {
			return
		}
		for {
			n, readErr := resp.Body.Read(buf)
			if n > 0 {
				_, _ = w.Write(buf[:n])
				if flusher != nil {
					flusher.Flush()
				}
			}
			if readErr != nil {
				if readErr != io.EOF {
					log.Printf("upstream stream read error: %v", readErr)
				}
				break
			}
		}
	}
}

func main() {
	port := flag.Int("port", 0, "localhost port")
	secret := flag.String("secret", "", "bearer secret")
	flag.Parse()
	if *port <= 0 || *secret == "" {
		log.Fatal("--port and --secret are required")
	}

	mux := http.NewServeMux()
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("OK"))
	})
	mux.HandleFunc("/request", requestHandler(*secret))
	mux.HandleFunc("/stream", streamHandler(*secret))

	server := &http.Server{
		Addr:              fmt.Sprintf("127.0.0.1:%d", *port),
		Handler:           mux,
		ReadHeaderTimeout: 10 * time.Second,
	}
	l, err := net.Listen("tcp", server.Addr)
	if err != nil {
		log.Fatal(err)
	}
	ctx := context.Background()
	go func() { <-ctx.Done() }()
	log.Fatal(server.Serve(l))
}
