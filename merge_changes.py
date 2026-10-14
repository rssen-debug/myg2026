"""Development-only merger; no network or trading."""
import ast
from pathlib import Path

ROOT = Path(__file__).parent
source = (ROOT / "godlike2026_original.py").read_text(encoding="utf-8-sig")

def replace_node(src, name, replacement, class_name=None):
    tree = ast.parse(src)
    nodes = tree.body
    if class_name:
        nodes = next(n for n in nodes if isinstance(n, ast.ClassDef) and n.name == class_name).body
    node = next(n for n in nodes if getattr(n, "name", None) == name)
    start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
    lines = src.splitlines(keepends=True)
    lines[start-1:node.end_lineno] = [replacement.rstrip() + "\n"]
    return "".join(lines)

def extract(src, node):
    start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
    return "\n".join(src.splitlines()[start-1:node.end_lineno])

def insert_methods(src, parent, fragment_class):
    tree = ast.parse(parts)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == fragment_class)
    for method in cls.body:
        if not isinstance(method, ast.FunctionDef):
            continue
        parent_node = next(n for n in ast.parse(src).body if isinstance(n, ast.ClassDef) and n.name == parent)
        if any(getattr(n, "name", None) == method.name for n in parent_node.body):
            src = replace_node(src, method.name, extract(parts, method), parent)
        else:
            lines = src.splitlines(keepends=True)
            lines.insert(parent_node.end_lineno, "\n" + extract(parts, method) + "\n")
            src = "".join(lines)
    return src

# Remove marketing claims and implicit wallet-key loading from offline operation.
doc = ast.parse(source).body[0]
lines = source.splitlines(keepends=True)
lines[doc.lineno-1:doc.end_lineno] = ['"""GODLIKE 2026: experimental paper trading and auditable exit replay.\n'
    'No demonstrated trading edge, guaranteed fills, anti-rug guarantee or live readiness.\n'
    'See README_SV.md for verified behavior, model assumptions and remaining limitations.\n"""\n']
source = "".join(lines)
start = source.index('# Försök ladda dotenv')
end = source.index('# Försök ladda solders', start)
source = source[:start] + source[end:]
source = source.replace('import base64', 'import base64\nimport threading\nfrom contextlib import contextmanager')
source = source.replace('"WALLET_PRIVATE_KEY": os.getenv("WALLET_PRIVATE_KEY", ""),', '"WALLET_PRIVATE_KEY": "",  # Paper build never loads private keys')
source = source.replace('"DRY_RUN": True,', '"DRY_RUN": True,\n    "MAX_MARKET_DATA_AGE_SECONDS": 30,\n    "SIGNAL_MIN_SECONDS": 30,\n    "MIN_VOLUME_H1_USD": D("25000"),')
source = source.replace('"https://lite-api.jup.ag/swap/v1/quote",\n        ', '').replace('        "https://public.jupiterapi.com/quote",\n', '').replace('"https://lite-api.jup.ag/swap/v1/swap",\n        ', '')
source = source.replace('class MarketDataUnavailableError', 'LIVE_BLOCK_REASON = "Livehandel spärrad: transaktionsvis avstämning, återstart och orderkontroller är inte verifierade. Denna version är endast för paper/replay."\n\nclass MarketDataUnavailableError')

parts = (ROOT / "root_parts.py").read_text(encoding="utf-8")
extra_funcs = []
names = {getattr(n,"name",None) for n in ast.parse(source).body}
for node in ast.parse(parts).body:
    if isinstance(node, ast.FunctionDef):
        if node.name in names:
            source = replace_node(source, node.name, extract(parts,node))
        else:
            extra_funcs.append(extract(parts,node))
source = source.replace('class StateManager:', '\n\n'.join(extra_funcs) + '\n\n\nclass StateManager:')
source = insert_methods(source, 'StateManager', 'StateUpdates')
source = insert_methods(source, 'GodlikeBot', 'BotUpdates')

# Every state operation uses its own run configuration, not module defaults.
for cls_name in ('StateManager', 'GodlikeBot'):
    node = next(n for n in ast.parse(source).body if isinstance(n,ast.ClassDef) and n.name==cls_name)
    code = extract(source,node)
    code = code.replace('CONFIG[', 'self.cfg[')
    source = replace_node(source, cls_name, code)

# Never seed invented identity/win-rate claims as trustworthy trading data.
start = source.index('            # Seed Cupsey')
end = source.index('            conn.commit()', start)
source = source[:start] + source[end:]

# Harden the remaining legacy open/reset implementations in the same DB transaction.
node = next(n for n in ast.parse(source).body if isinstance(n,ast.ClassDef) and n.name=='StateManager')
opening = extract(source, next(n for n in node.body if getattr(n,'name',None)=='record_open_position'))
validation = '''        raw_value = dec(raw_amount)
        usd_invested, price_usd, fee_usd = dec(usd_invested), dec(price_usd), dec(fee_usd)
        net_token_value = dec(net_token_value)
        if raw_value <= ZERO or raw_value != raw_value.to_integral_value():
            raise ValueError("Köp kräver positiva heltal raw tokens")
        if usd_invested <= ZERO or price_usd <= ZERO or fee_usd < ZERO or net_token_value <= ZERO:
            raise ValueError("Ogiltig köpbokföring")
        if isinstance(decimals, bool) or not isinstance(decimals, int) or not 0 <= decimals <= 18:
            raise ValueError("Ogiltiga decimals")
        raw_amount = str(int(raw_value))
'''
opening = opening.replace('        now = time.time()', validation + '        now = time.time()', 1)
opening = opening.replace('            c = conn.cursor()', '''            conn.execute("BEGIN IMMEDIATE")
            c = conn.cursor()
            if conn.execute("SELECT 1 FROM positions WHERE mint=? AND is_paper=? AND status='OPEN'", (mint, int(is_paper))).fetchone():
                raise ValueError("Position redan öppen")
            if is_paper:
                current = conn.execute("SELECT cash_usd FROM wallet WHERE id=1").fetchone()
                if dec(current[0]) < usd_invested:
                    raise ValueError("Otillräcklig kassa")''', 1)
source = replace_node(source, 'record_open_position', opening, 'StateManager')
node = next(n for n in ast.parse(source).body if isinstance(n,ast.ClassDef) and n.name=='StateManager')
reset = extract(source, next(n for n in node.body if getattr(n,'name',None)=='reset_wallet'))
reset = reset.replace('        with self._get_connection()', '''        initial_cash = dec(initial_cash)
        if initial_cash <= ZERO:
            raise ValueError("Startkapital måste vara positivt")
        with self._get_connection()''',1)
reset = reset.replace('            c.execute("DELETE FROM positions', '''            c.execute("UPDATE wallet SET peak_equity_usd=? WHERE id=1", (str(initial_cash),))
            c.execute("DELETE FROM equity_history")
            c.execute("DELETE FROM market_observations")
            c.execute("DELETE FROM positions''',1)
source = replace_node(source, 'reset_wallet', reset, 'StateManager')

# Remove broken transaction broadcasting/reconciliation rather than a bypassable CLI-only warning.
source = replace_node(source,'execute_live_swap', '''    def execute_live_swap(self, quote, in_mint, out_mint, pos_id=0, direction="SWAP", amount_raw=0,
                          expected_usd=ZERO, symbol="", pair_address="", decimals=6):
        return False, "", LIVE_BLOCK_REASON, ZERO''', 'JupiterEngine')
source = replace_node(source,'_load_keypair', '''    def _load_keypair(self):
        self.keypair = None  # No signing keys are loaded by this paper build.''','JupiterEngine')

for name, filename in [('PositionMonitor','monitor_patch.py'),('RugCheckValidator','rug_patch.py'),('run_real_solana_replay','replay_patch.py')]:
    fragment=(ROOT/filename).read_text(encoding='utf-8-sig')
    source=replace_node(source,name,fragment)

source = source.replace('TEST MODE  (Paper Trading — 30 minuter, Profit Check)', 'TEST MODE  (Paper / CSV — simulerat resultat)')
source = source.replace('LIVE MODE  (Skarp on-chain handel med riktiga medel)', 'LIVE MODE  (Spärrat i denna granskningsversion)')
source = source.replace('Garanterar 100% precision utan risk för felaktiga DexScreener-par.', 'Indicativ exekveringsquote, inte en exakt USD-markering. Legacy API kräver validerad integration.')
source = source.replace('Verifierade och aktiva Jupiter Swap v1 API-endpoints', 'Legacy Jupiter v1-referenser; live är spärrat och API-integrationen ej verifierad')
source = source.replace('Kör fullständig institutional RugCheck validering på en Solana mintadress.', 'Granskar RugCheck-rapporten för en uttryckligen vald pool; ingen säkerhetsgaranti.')
source = source.replace('Kör samtliga 7 institutionella säkerhetstester i isolerad databas.', 'Kör offline-regressionstester med isolerade fixtures; ingen live-certifiering.')
source = source.replace('Säkerhetsaudit:', 'Offline-kodtester:').replace('Säker: {safe}', 'Filter godkänt: {safe}')
# MCP must return the same liquidation-aware report and blocked live status.
start = source.index('                    open_pos = self.bot.state.get_open_positions', source.index('class MCPServer:'))
end = source.index('                    return {"jsonrpc"', start)
source = source[:start] + '''                    if is_paper:
                        res_text = json.dumps(self.bot.state.performance_report(self.bot.dex), default=str, ensure_ascii=False)
                    else:
                        res_text = LIVE_BLOCK_REASON
''' + source[end:]
source = source.replace('self.bot.state.reset_wallet(D("100.00"))', 'self.bot.state.reset_wallet(self.bot.cfg["INITIAL_CASH_USD"])')
source = source.replace('"Paper wallet återställd till $100.00 USD."', 'f"Ny paperhistorik med startkassa ${self.bot.cfg[\'INITIAL_CASH_USD\']:.2f}. Inga verkliga säljorder gjordes."')
source = source.replace('Nollställer simulator-kassan till $100.00 USD och stänger alla öppna positioner.', 'Raderar paperhistoriken och återställer konfigurerat startkapital. Detta är ingen försäljning.')
source = source.replace('Skannar Solana efter trendande tokens och utvärderar Trench IQ & RugCheck.', 'Läser trendande Solana-tokens och riskrapporter; detta är inte verifierade köpsignaler.')
source = source.replace('Lås in +3% nettovinst vid breakeven', 'Prisbaserad stopnivå +3%; ingen garanti om nettovinst')
source = source.replace('Nödfrysning om portföljen tappar 20%', 'Spärra nya köp vid 20% drawdown från observerad topp')
compile(source, str(ROOT/'godlike2026.py'), 'exec')
(ROOT/'godlike2026.py').write_text(source,encoding='utf-8')
print('Merged:',ROOT/'godlike2026.py')
