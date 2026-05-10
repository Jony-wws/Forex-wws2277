export default function ErrorBanner({
  error,
  onRetry,
}: {
  error: Error;
  onRetry?: () => void;
}) {
  return (
    <div
      role="alert"
      className="card border-sell/40 bg-sell/5 p-4 text-sm animate-fadeIn"
    >
      <div className="font-semibold text-sell mb-1">Ошибка загрузки</div>
      <div className="text-muted mb-3 break-words">{error.message}</div>
      {onRetry && (
        <button className="btn-primary" onClick={onRetry} type="button">
          Повторить
        </button>
      )}
    </div>
  );
}
