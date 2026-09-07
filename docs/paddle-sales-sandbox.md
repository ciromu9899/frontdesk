# Paddle販売サービス：Sandbox実装

販売者専用。購入者へ配布するFrontDesk本体とは別プロセスです。
本番キーは拒否し、APIはsandbox-api.paddle.comに固定。公開販売はまだ有効化していません。
SDK追加なしで公式のraw body + HMAC-SHA256 + 5秒許容時刻検証を実装しています。

## 実装済み

- ブラウザー購入画面 `/` とPaddle.js sandbox checkout。
- POST `/session`：256 bitの受取キーを発行、DBにはハッシュのみ保存。24時間期限。
- POST `/checkout`：設定済み単一price_id、数量1の取引を作成。価格や顧客IDはリクエストから受け取らない。
- POST `/webhook`：署名検証後、イベントID・契約状態をSQLiteに原子的に保存。古い状態は適用しない。同時刻の矛盾はエラーとして手動照合が必要。
- GET `/status`、`/download`：Authorization bearer受取キーが必須。支払完了、価格、数量、顧客一致、契約状態、有効期限、返金保留を確認。
- ZIPは設定したファイルのみ配布し、設定SHA-256との一致も検証。
- GET `/portal`：PaddleのSandboxメール認証ポータルへ移動。ログイン済みポータルセッションは発行せず、Paddleで購入者のメール認証を要求する。
- 期間末の解約予約はその期限まで納品可能。canceled/paused/past_dueや期限切れは納品停止。
- API作成のタイムアウトは自動再試行しない。二重契約防止のため手動照合が必要。

この権限制御は新たなZIP取得の制御です。既に納品したApache-2.0ソースやインストール済みアプリを遠隔停止しません。

## 起動準備（未設定・未実施）

Sandbox専用商品・定期価格、APIキー、公開クライアントトークン、Webhook destinationを用意します。
次の値はシークレット管理機構からプロセス環境に渡してください。チャット・Git・ログへ貼らないでください。

- SHELLIE_PADDLE_SANDBOX_API_KEY
- SHELLIE_PADDLE_SANDBOX_CLIENT_TOKEN
- SHELLIE_PADDLE_SANDBOX_WEBHOOK_SECRET

```
python paddle_sales_server.py --database <新規Sandbox専用DBパス> --package <ZIPパス> --sha256 <SHA256> --price-id <Sandboxのpri_ID> --portal-url <Sandboxのメールログイン用ポータルURL>
```

127.0.0.1:8782のみで待受。購入画面はこのローカルURLから操作。
実Webhookを届ける際はTLSリバースプロキシ経由で `/webhook` だけを外部へ公開してください。
秘密情報・本番DBを変更する前に別途確認を取ること。DB親ディレクトリは事前に用意してください。
Paddle Checkoutにはデフォルト支払ページの設定も必要です。

購読対象: transaction.completed/updated/payment_failed、subscription.created/updated/activated/trialing/past_due/paused/resumed/canceled、adjustment.created/updated。

## 販売前の未完了事項

- 実SandboxのCheckout→署名Webhook→実ZIP納品→Paddleポータル解約のE2E。
- 長期ログイン・端末間の受取キー復旧、本人確認とアカウント管理。
- 更新課金・複数取引にまたがる部分返金/チャージバックの詳細ポリシーと検証。
- 決済情報の照合・イベント再取得、曖昧なAPI応答の復旧、既存顧客のバックフィル。
- 本番サービス用の認証、レート制限、CSP、監視、バックアップ、DB保存期間。
- 本番プラン/規約/税表示/サポート範囲の確定、販売サイトへの公開接続。

このファイルと販売バックエンドは顧客用ZIPから除外します。

## テスト

`python -m unittest discover -s tests -p test_paddle_sales.py -v`

APIはモック、HTTPはloopbackのみ、DBは一時領域。カード課金や実契約の変更はありません。
本番公開の証拠ではありません。

公式仕様: https://developer.paddle.com/webhooks/about/signature-verification/
ポータル: https://developer.paddle.com/api-reference/customer-portals/create-customer-portal-session/
