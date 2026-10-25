-- figcaption.lua: bind a figure image to its caption paragraph.
--
-- Handles the two shapes the OCR markdown produces:
--   (a) one Para: [Image, LineBreak, "Рисунок [N] ..."]   (hard break)
--   (b) Para [Image] followed by a Para starting with the caption words
-- and rewrites them to \FigWithCaption{<path>}{<caption latex>} — a
-- centered image with a \small caption whose width follows the image
-- (defined in the preamble next to the \FigMeasureBox auto-fit wrapper).
-- LaTeX auto-numbering stays off: the numbers already live in the captions.

local function starts_caption(inlines)
  local s = pandoc.utils.stringify(inlines)
  return s:find('Рисунок [', 1, true) == 1
      or s:find('Рис.', 1, true) == 1
      or s:find('Figure [', 1, true) == 1
      or s:match('^Рисунок%s+%d') ~= nil
      or s:match('^Figure%s+%d') ~= nil
      or s:match('^Fig%.%s*%d') ~= nil
end

local function latex_of_inlines(inls)
  local doc = pandoc.Pandoc({ pandoc.Plain(inls) })
  return pandoc.write(doc, 'latex'):gsub('%s+$', '')
end

local function fig_block(src, cap_inlines)
  return pandoc.RawBlock('latex', string.format(
    '\\FigWithCaption{%s}{%s}', src, latex_of_inlines(cap_inlines)))
end

-- Default-width pipe tables otherwise become natural-width l/r/c columns.
-- Three page-sized images can then make the table wider than the page:
-- those columns do not establish a cell-local linewidth for the image cap.
function Table(tbl)
  local missing = 0
  for _, spec in ipairs(tbl.colspecs) do
    if spec[2] == nil or spec[2] == 0 then missing = missing + 1 end
  end
  if missing == #tbl.colspecs and missing > 0 then
    local specs = tbl.colspecs
    for _, spec in ipairs(specs) do spec[2] = 1 / missing end
    tbl.colspecs = specs
  end
  return tbl
end

local function is_gap(inline)
  return inline.t == 'Space' or inline.t == 'SoftBreak' or inline.t == 'LineBreak'
end

-- A justified paragraph containing many differently sized image boxes can
-- choose an overfull line to avoid extremely stretched spaces. Give image
-- runs their own ragged paragraph; this also works inside a footnote or cell.
-- Text before/after the run and each original Image node stay in order.
local function split_image_runs(block)
  if block.t ~= 'Para' and block.t ~= 'Plain' then return nil end
  local out, text, i, changed = pandoc.Blocks({}), pandoc.List({}), 1, false
  local function flush_text()
    while #text > 0 and is_gap(text[#text]) do text:remove(#text) end
    if #text > 0 then out:insert(block.t == 'Para' and pandoc.Para(text) or pandoc.Plain(text)) end
    text = pandoc.List({})
  end
  while i <= #block.content do
    if block.content[i].t == 'Image' then
      local j, last, count = i, i, 0
      while j <= #block.content do
        if block.content[j].t == 'Image' then count = count + 1; last = j
        elseif not is_gap(block.content[j]) then break end
        j = j + 1
      end
      if count >= 2 then
        flush_text()
        local run = pandoc.List({})
        for k = i, last do run:insert(block.content[k]) end
        out:insert(pandoc.RawBlock('latex', '\\begingroup\\raggedright\n'
          .. latex_of_inlines(run) .. '\n\\par\\endgroup'))
        changed, i = true, last + 1
        while i <= #block.content and is_gap(block.content[i]) do i = i + 1 end
      else text:insert(block.content[i]); i = i + 1 end
    else text:insert(block.content[i]); i = i + 1 end
  end
  flush_text()
  return changed and out or nil
end

function Blocks(blocks)
  local out = pandoc.Blocks({})
  local i = 1
  while i <= #blocks do
    local b, nxt = blocks[i], blocks[i + 1]
    local handled = false
    -- (a) image + hard break + caption inside one paragraph
    if b.t == 'Para' and #b.content >= 3
        and b.content[1].t == 'Image' and b.content[2].t == 'LineBreak' then
      local cap = pandoc.List({})
      for j = 3, #b.content do cap:insert(b.content[j]) end
      if starts_caption(cap) then
        out:insert(fig_block(b.content[1].src, cap))
        handled = true
        i = i + 1
      end
    end
    -- (b) lone image paragraph + separate caption paragraph
    if not handled and b.t == 'Para' and #b.content == 1
        and b.content[1].t == 'Image'
        and nxt and nxt.t == 'Para' and starts_caption(nxt.content) then
      out:insert(fig_block(b.content[1].src, nxt.content))
      handled = true
      i = i + 2
    end
    if not handled then
      local split = split_image_runs(b)
      if split then out:extend(split) else out:insert(b) end
      i = i + 1
    end
  end
  return out
end
