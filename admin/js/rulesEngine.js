// Comparison engine — the single client-side implementation of rule matching,
// mirroring compare() in app/rules.py (keep the two in sync). Returns
// {match, error}: `error` is a human-readable reason when the comparison could
// not be performed (bad regex, non-numeric threshold, unrecognisable boolean
// word); null whenever the comparison ran, matched or not. Used by the rules
// editor's "Test rules" so failures explain themselves in the dock terminal.

// Words treated as boolean true/false by the is / is_not operators (mirrors
// app/rules.py). Anything else fails to normalise and never matches.
const TRUE_WORDS = new Set(['1', 'true', 'yes', 'on', 'up', 'ok']);
const FALSE_WORDS = new Set(['0', 'false', 'no', 'off', 'down']);

function asBool(s) {
  const low = String(s == null ? '' : s).trim().toLowerCase();
  if (TRUE_WORDS.has(low)) return true;
  if (FALSE_WORDS.has(low)) return false;
  return null;
}

// Leading numeric prefix of *text* as a number ("5%" -> 5), or null. Mirrors
// _leading_num in app/rules.py: comparison ops fall back to this when the value
// has no parsed num (str cast, or regex captured "5%"), so units don't break
// gt/lt/gte/lte/between.
function leadingNum(text) {
  const m = /^\s*(-?\d+(?:\.\d+)?)/.exec(String(text ?? ''));
  return m ? parseFloat(m[1]) : null;
}

// True when *text*'s leading numeric prefix equals cnum — mirrors
// _text_num_equal in app/rules.py so eq/ne match percentage-style values
// ("85 %", rule "85%") that never compare equal as raw strings.
function textNumEqual(text, cnum) {
  const lead = leadingNum(text);
  return lead !== null && lead === cnum;
}

// Parse a 'min, max' range like _parse_range in app/rules.py: any run of
// commas and/or whitespace separates the two numbers. Returns [lo, hi] or null.
function parseRange(rv) {
  const parts = String(rv ?? '').trim().split(/[,\s]+/).filter(Boolean);
  if (parts.length !== 2) return null;
  const lo = parseFloat(parts[0]);
  const hi = parseFloat(parts[1]);
  if (Number.isNaN(lo) || Number.isNaN(hi)) return null;
  return [lo, hi];
}

// Evaluate one operator against *value* ({num, txt, raw, error}).
export function compare(op, ruleValue, value) {
  if (op === 'missing') return { match: !!(value && value.error), error: null };
  if (op === 'present') return { match: !!(value && !value.error), error: null };
  if (!value || value.error) return { match: false, error: null };
  const rv = String(ruleValue ?? '');
  const text = value.txt != null ? value.txt : (value.raw || '');
  // Mirror backend _coerce: no parsed num -> use the text's leading numeric
  // prefix so "5%" still evaluates against gt/lt/gte/lte/between rules.
  const num = typeof value.num === 'number' ? value.num : leadingNum(text);
  const cnumParsed = parseFloat(rv);
  const cnum = rv !== '' && !Number.isNaN(cnumParsed) ? cnumParsed : null;
  switch (op) {
    case 'gt':
    case 'lt':
    case 'gte':
    case 'lte': {
      if (cnum === null) return { match: false, error: `rule value '${rv}' is not numeric` };
      if (num === null) return { match: false, error: `value '${text}' is not numeric` };
      if (op === 'gt') return { match: num > cnum, error: null };
      if (op === 'lt') return { match: num < cnum, error: null };
      if (op === 'gte') return { match: num >= cnum, error: null };
      return { match: num <= cnum, error: null };
    }
    case 'eq':
      if (cnum !== null) {
        return { match: (num !== null && num === cnum) || textNumEqual(text, cnum), error: null };
      }
      return { match: text === rv, error: null };
    case 'ne':
      if (cnum !== null) {
        return { match: !((num !== null && num === cnum) || textNumEqual(text, cnum)), error: null };
      }
      return { match: text !== rv, error: null };
    case 'contains':
      return { match: text.includes(rv), error: null };
    case 'regex':
      try { return { match: new RegExp(rv).test(text), error: null }; }
      catch (e) { return { match: false, error: `invalid regex: ${e.message}` }; }
    case 'is':
    case 'is_not': {
      const want = asBool(rv);
      if (want === null) return { match: false, error: `cannot interpret '${rv}' as true/false` };
      let got = asBool(text);
      if (got === null && num !== null) got = num !== 0;
      if (got === null) return { match: false, error: `cannot interpret value '${text}' as true/false` };
      return { match: op === 'is' ? got === want : got !== want, error: null };
    }
    case 'between': {
      const range = parseRange(rv);
      if (!range) return { match: false, error: "expected two numbers 'min, max'" };
      if (num === null) return { match: false, error: `value '${text}' is not numeric` };
      return { match: num >= range[0] && num <= range[1], error: null };
    }
    default:
      return { match: false, error: `unknown operator '${op}'` };
  }
}
