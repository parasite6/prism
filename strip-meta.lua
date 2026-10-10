-- Drop all document metadata (title, author, date, keywords...) so none of it
-- reaches the output: no front matter, title block, or <meta> tags.
function Meta(meta)
  return pandoc.Meta({})
end
