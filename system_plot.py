"""Account comparisons reuse the existing drawdown calculation."""
from datetime import datetime
from pathlib import Path
from plot_backtest import drawdown


def render_evening(result, fills, folder):
    """Saved close/MA chart with historical paper fills, no inference on reopening."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    points=result['points']
    fig,ax=plt.subplots(figsize=(10,4),constrained_layout=True)
    dates=[datetime.fromisoformat(p['day']) for p in points]
    for key in ('close','ma5','ma20'):
        ax.plot(dates,[p[key] for p in points],label=key)
    for side,marker in [('BUY','^'),('SELL','v')]:
        selected=[f for f in fills if f['symbol']==result['symbol'] and f['side']==side
                  and points[0]['day']<=f['at'][:10]<=points[-1]['day']]
        ax.scatter([datetime.fromisoformat(f['at']).replace(tzinfo=None) for f in selected],
                   [f['price'] for f in selected],marker=marker,label=side)
    ax.set_title(result['symbol']+' / held '+str(result['held'])+' shares / raw price units')
    ax.grid(alpha=.2);ax.legend()
    fig.savefig(Path(folder)/(result['symbol']+'.png'),dpi=110)
    plt.close(fig)


def render(reports, bars, folder):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    folder = Path(folder)
    fig, axes = plt.subplots(3,1,figsize=(12,10), constrained_layout=True)
    for name,r in reports.items():
        dates = [datetime.fromisoformat(e['day']) for e in r['equity']]
        values = [e['equity'] for e in r['equity']]
        axes[0].plot(dates,values,label=name)
        axes[1].plot(dates,drawdown(values,r['parameters']['initial']),label=name)
    axes[0].set(ylabel='Equity (JPY)', title='Shared cash / research reconstruction / after costs')
    axes[1].set(ylabel='Drawdown (%)')
    axes[2].bar(list(reports),[r['net_pnl'] for r in reports.values()])
    axes[2].set(ylabel='Net P&L (JPY)')
    for ax in axes:
        ax.grid(alpha=.2)
    axes[0].legend()
    fig.savefig(folder/'comparison.png',dpi=140)
    plt.close(fig)
    # Individual plots in raw daily price units; splits remain visibly marked.
    for symbol in sorted({b.symbol for b in bars}):
        series = [b for b in bars if b.symbol==symbol]
        fig,axes = plt.subplots(len(reports),1,figsize=(12,3*len(reports)),squeeze=False,constrained_layout=True)
        for ax,(name,r) in zip(axes[:,0],reports.items()):
            ax.plot([datetime.fromisoformat(b.day) for b in series],[b.close for b in series],label='Raw close')
            for side,marker in [('BUY','^'),('SELL','v')]:
                fills=[f for f in r['fills'] if f['symbol']==symbol and f['side']==side]
                ax.scatter([datetime.fromisoformat(f['at']).replace(tzinfo=None) for f in fills],
                           [f['price'] for f in fills],marker=marker,label=side)
            for b in series:
                if b.split_factor!=1:
                    ax.axvline(datetime.fromisoformat(b.day),color='gray',linestyle=':')
            ax.set_title(symbol+' / '+name)
            ax.legend()
        fig.savefig(folder/(symbol+'.png'),dpi=120)
        plt.close(fig)
