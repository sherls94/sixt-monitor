import { ExternalLink, Trophy } from 'lucide-react';
import { Card } from '@/components/ui/card';
import { ProviderBadge } from '@/components/ProviderBadge';
import type { ProviderResult } from '@/lib/types';

export function ProviderResultCard({ r, isCheapest = false }: { r: ProviderResult; isCheapest?: boolean }) {
  return (
    <Card className={`p-4 flex items-center gap-3 ${isCheapest ? 'bg-success/10 ring-1 ring-success/40' : ''}`}>
      <ProviderBadge provider={r.provider} size={44} />

      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <span className="font-medium text-foreground truncate">{r.provider}</span>
          {isCheapest && <Trophy className="h-3.5 w-3.5 text-success shrink-0" />}
        </div>
        {r.status === 'ok' ? (
          <div className="text-xs text-muted-foreground truncate mt-0.5">{r.model || r.car_class}</div>
        ) : (
          <div className="text-xs text-muted-foreground mt-0.5">{r.status === 'na' ? 'Not available here' : 'Check failed'}</div>
        )}
      </div>

      <div className="flex items-center gap-3 shrink-0">
        {r.price != null && (
          <div className="text-right">
            <div className={`font-semibold ${isCheapest ? 'text-success' : 'text-foreground'}`}>${r.price.toFixed(2)}</div>
            {r.saving != null && r.saving >= 15 && (
              <div className="text-xs text-success">save ${r.saving.toFixed(2)}</div>
            )}
          </div>
        )}
        {r.booking_url && (
          <a href={r.booking_url} target="_blank" rel="noreferrer" aria-label={`Continue to ${r.provider}`}>
            <ExternalLink className="h-4 w-4 text-muted-foreground hover:text-foreground" />
          </a>
        )}
      </div>
    </Card>
  );
}
