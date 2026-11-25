export function BrowserReadUrlsReview({ urls }: { urls?: readonly string[] }) {
  if (!urls?.length) return null;
  return <>
    <dt>Exact pages Supymem may read</dt>
    <dd><ol>{urls.map(url => <li key={url}>{url}</li>)}</ol>
      Supymem may navigate directly to these pages within the website scope above. No input access is granted.
    </dd>
  </>;
}
