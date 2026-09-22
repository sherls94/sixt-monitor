import { providerLogo } from '@/lib/providers';

export function ProviderBadge({ provider, size = 28 }: { provider: string; size?: number }) {
  const logo = providerLogo(provider);
  if (!logo) {
    return (
      <div
        className="shrink-0 rounded-lg bg-secondary flex items-center justify-center font-bold text-foreground"
        style={{ width: size, height: size, fontSize: size * 0.5 }}
      >
        {provider.charAt(0)}
      </div>
    );
  }
  return (
    <div
      className="shrink-0 rounded-lg flex items-center justify-center overflow-hidden p-1"
      style={{ width: size, height: size, background: logo.lightBg ? '#FFFFFF' : 'transparent' }}
    >
      <img src={logo.src} alt={provider} className="max-h-full max-w-full object-contain" />
    </div>
  );
}
