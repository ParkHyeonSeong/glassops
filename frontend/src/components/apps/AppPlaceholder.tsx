interface AppPlaceholderProps {
  appId: string;
  title: string;
}

export default function AppPlaceholder({ appId, title }: AppPlaceholderProps) {
  return (
    <div className="app-placeholder">
      <p className="app-placeholder-title">{title}</p>
      <p className="app-placeholder-id">{appId}</p>
      <p className="app-placeholder-note">Phase 2+ 에서 구현 예정</p>
    </div>
  );
}
